"""`users` / `personal_access_tokens` 表 + 认证路由的请求/响应模型（M9 §2.4 / §2.12 / §2.15）。

**表结构住在 app 层**（`app.gateway.auth`），不进 `agentkit.persistence`：
harness 只认识 `CurrentUser` Protocol（有 `.id: str`），persistence 读 ContextVar、
gateway 写——依赖方向不能反。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.authz import ADMIN_ROLE, DEFAULT_ROLE
from agentkit.persistence.base import Base
from backend.app.gateway.auth.errors import InvalidEmail, InvalidUsername

#: §2.4：M9 只承认这两个角色。细粒度 RBAC 见 §12。
SYSTEM_ROLES = (ADMIN_ROLE, DEFAULT_ROLE)

#: PAT 明文前缀。库里只存 `sha256(token)` + 前 8 位，列表显示成 `dfp_ab12…`（§2.15）。
PAT_PREFIX = "dfp_"
PAT_DISPLAY_PREFIX_CHARS = 8

#: PAT 的 scope 是**固定菜单**，不接受自定义字符串（§2.15）。
PAT_SCOPE_MENU = frozenset({
    "runs:create",
    "runs:read",
    "runs:cancel",
    "threads:read",
    "threads:write",
    "threads:delete",
    "models:read",
    "skills:read",
    "skills:write",
    "memory:read",
    "memory:write",
    "knowledge:read",
    "knowledge:write",
})


class UserRow(Base):
    """账号。

    **`id` 是 uuid，不是邮箱也不是用户名**：它是 JWT 的 `sub`，也是存储桶的来源
    （`make_safe_user_id(users.id)`）。改邮箱、改用户名都**不能让桶名跟着变**（§2.2）——
    用户名比邮箱更危险：它看起来"本来就是 id"，很容易被顺手拿去拼路径。

    `email` 与 `username` 是**两个独立的登录标识**（各自 unique、各自小写归一）。
    登录接受 `identifier`：带 `@` 当邮箱查，否则当用户名查——两个字符集不重叠
    （用户名不允许 `@`），所以这个判断永远不歧义。

    `system_role` **没有**唯一索引：§2.7 提到的"admin 部分唯一索引"会禁止第二个 admin，
    而 §2.13 明确要求"不能把最后一个 admin 降级"——那前提就是可以有多个 admin。
    并发初始化改用条件插入（`WHERE NOT EXISTS (admin)`），降级/删除用条件写。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    #: 登录标识之一，也是"给人看的名字"。写入时小写归一（§2.4）。
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    system_role: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_ROLE)
    needs_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_users_system_role", "system_role"),)


class PersonalAccessTokenRow(Base):
    """PAT：**只存 digest**。明文只在创建响应里出现一次（§2.15）。"""

    __tablename__ = "personal_access_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 签发那一刻的 `users.token_version`。PAT 与 session **共享**这个撤销机制：
    #: 改密 / 重置 / 软删都 +1，于是所有 PAT 一起失效（§2.14 的失效矩阵）。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


_EMAIL_MAX = 254

#: 请求体字段的**兜底**上界：比业务规则宽，只用来挡住荒谬的 body。
#: 业务规则（邮箱 254 字符、用户名 32 字符、口令 72 字节）在领域层报具体错误。
_FIELD_GUARD = 512
_PASSWORD_GUARD = 4096


def normalize_email(raw: str) -> str:
    """邮箱唯一性按小写比较；身份本身（`users.id`）与邮箱无关。

    **每条规则单独报错**：模型层不再用 `min_length` / 自定义 validator 抢答，
    否则用户只会拿到 pydantic 的 `Request validation failed`——
    知道"错了"但不知道"错在哪"。这里抛的是带 `code` 的 422，detail 说清规则，
    并回显调用方刚敲进来的那个值（回显输入安全，回显库里的数据不安全）。
    """
    if not isinstance(raw, str):
        raise InvalidEmail(f"email must be a string, got {type(raw).__name__}")
    value = raw.strip().lower()
    if not value:
        raise InvalidEmail("email must not be empty")
    if len(value) > _EMAIL_MAX:
        raise InvalidEmail(f"email must be at most {_EMAIL_MAX} characters (got {len(value)})")
    if any(ch.isspace() for ch in value):
        raise InvalidEmail(f"email {raw!r} must not contain whitespace")
    if value.count("@") != 1:
        raise InvalidEmail(f"email {raw!r} must contain exactly one '@'")
    local, sep, domain = value.rpartition("@")
    if not sep:
        raise InvalidEmail(f"email {raw!r} is missing '@'")
    if not local:
        raise InvalidEmail(f"email {raw!r} is missing the part before '@'")
    if not domain:
        raise InvalidEmail(f"email {raw!r} is missing the domain after '@'")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise InvalidEmail(f"email {raw!r} must have a domain with a dot, like example.com")
    if ".." in value:
        raise InvalidEmail(f"email {raw!r} must not contain '..'")
    return value


_USERNAME_MIN = 3
_USERNAME_MAX = 32
_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{1,30}[a-z0-9])$")

#: 这些名字在日志、桶名、运行期上下文里有特殊含义，不能给用户注册。
RESERVED_USERNAMES = frozenset({"default", "anonymous", "auth-disabled", "system", "internal"})


def normalize_username(raw: str) -> str:
    """用户名归一 + 逐条报错（§2.4）。

    大小写不敏感：`Alice` 会被接受并写成 `alice`。**每条规则单独报错**，
    用户要知道是"太短""有非法字符"还是"首尾不能是符号"，而不是一句"用户名不合法"。
    """
    if not isinstance(raw, str):
        raise InvalidUsername(f"username must be a string, got {type(raw).__name__}")
    value = raw.strip().lower()
    if not value:
        raise InvalidUsername("username must not be empty")
    if len(value) < _USERNAME_MIN or len(value) > _USERNAME_MAX:
        raise InvalidUsername(
            f"username must be {_USERNAME_MIN}-{_USERNAME_MAX} characters (got {len(value)})"
        )
    if not _USERNAME_RE.match(value):
        raise InvalidUsername(
            f"username {raw!r} must use letters, digits, '_', '.' or '-', "
            "and must start and end with a letter or digit"
        )
    if value in RESERVED_USERNAMES:
        raise InvalidUsername(f"username {value!r} is reserved")
    return value


def normalize_identifier(raw: str) -> str:
    """登录标识：带 `@` 当邮箱，否则当用户名。返回的是**规范化之后**的查库键。"""
    if not isinstance(raw, str):
        raise InvalidUsername(f"identifier must be a string, got {type(raw).__name__}")
    if "@" in raw:
        return normalize_email(raw)
    return normalize_username(raw)


class LoginRequest(BaseModel):
    """登录只认 **identifier**：邮箱或用户名。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: 旧的 `email` 字段名仍然接受，新客户端只发 `identifier`。
    #: 上界是**防超大 body**用的，不是业务规则——"太长"由领域层报具体错误。
    identifier: str = Field(
        min_length=1, max_length=_FIELD_GUARD, validation_alias=AliasChoices("identifier", "email")
    )
    #: 登录只比对 hash，不做策略校验：100 字节的口令该得到"密码不对"，
    #: 空口令也一样（`verify_password` 返回 False）——而不是一句"格式错"。
    password: str = Field(max_length=_PASSWORD_GUARD)
    remember_me: bool = False


class _RegistrationRequest(BaseModel):
    """注册 / 初始化的共同形状。形状只做**粗**校验（防超大 body），细则交给领域层：

    模型层的 `max_length` 会先抛 pydantic 的 `Request validation failed`，
    用户只知道"错了"、不知道"错在哪"。所以这里的上界给得**比业务规则宽**，
    让 `normalize_email` / `normalize_username` / `validate_password_policy`
    去报 `invalid_email` / `invalid_username` / `password_too_short`。
    """

    model_config = ConfigDict(extra="forbid")

    # 连 `min_length=1` 都不加：空串是**规则**问题，让 `normalize_*` 报
    # `invalid_email` / `invalid_username` / `invalid_password`（说清是哪一条），
    # 而不是 pydantic 那句"Request validation failed"。
    email: str = Field(max_length=_FIELD_GUARD)
    username: str = Field(max_length=_FIELD_GUARD)
    password: str = Field(max_length=_PASSWORD_GUARD)


class InitializeRequest(_RegistrationRequest):
    pass


class RegisterRequest(_RegistrationRequest):
    pass


class UsernameUpdateRequest(BaseModel):
    """`PATCH /api/v1/auth/me` 的请求体（§2.12）。

    **只允许改用户名**，而且改的是调用者自己（会话里那个人）——路径里没有 user_id，
    所以没有 IDOR 的余地。邮箱、角色、密码各有各的路径，不在这里顺手一起改：
    一个能改多件事的 PATCH 迟早会出现"我只想改用户名，怎么把邮箱也改了"。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=_FIELD_GUARD)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 空口令走 `verify_password` / `validate_password_policy`：
    # 前者给 401 "current password is incorrect"，后者给 422 "must be non-empty"。
    current_password: str = Field(max_length=_PASSWORD_GUARD)
    new_password: str = Field(max_length=_PASSWORD_GUARD)


class UserResponse(BaseModel):
    """没有 `password_hash`，没有 `token_version`，没有 `disabled`。"""

    id: str
    email: str
    username: str
    system_role: Literal["admin", "user"]


class LoginResponse(BaseModel):
    """**没有 token 字符串**（§2.4）。Set-Cookie 由 `session_cookie.py` 写。"""

    expires_in: int
    needs_setup: bool


class SetupStatusResponse(BaseModel):
    needs_setup: bool


class ProvidersResponse(BaseModel):
    local: bool = True


class OkResponse(BaseModel):
    ok: bool = True


def user_to_wire(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "username": row["username"],
        "system_role": row["system_role"],
    }
