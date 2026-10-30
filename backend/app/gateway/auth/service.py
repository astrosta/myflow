"""账号生命周期 + 会话 + PAT（M9 §2.4 / §2.13 / §2.14 / §2.15）。

**账号的每一次状态变化都要回答两个问题：令牌还有效吗？数据还看得见吗？**
这个类的每个方法就是那张表的一行；路由层只做翻译，不做判断。

撤销只有两个机制（§2.14）：`token_version`（服务端计数）与 `disabled` / `deleted_at`
（服务端状态）。没有黑名单、没有 Redis——**登出不是全局登出**。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from agentkit.utils import make_safe_user_id
from agentkit.persistence.base import iso_utc
from backend.app.gateway.auth.buckets import mark_user_bucket_deleted, unmark_user_bucket
from backend.app.gateway.auth.errors import (
    AccountDeleted,
    AccountDisabled,
    AccountNotFound,
    AdminAlreadyExists,
    CannotModifySelf,
    CodedError,
    InvalidCredentials,
    InvalidScope,
    LastAdmin,
    LoginRateLimited,
    RegistrationClosed,
    TooManyPats,
    UserAlreadyExists,
    UserNotFound,
    UsernameTaken,
    WrongPassword,
)
from backend.app.gateway.auth.jwt import encode_access_token
from backend.app.gateway.auth.login_limit import LoginRateLimiter, login_rate_limiter
from backend.app.gateway.auth.models import (
    PAT_DISPLAY_PREFIX_CHARS,
    PAT_PREFIX,
    PAT_SCOPE_MENU,
    SYSTEM_ROLES,
    normalize_email,
    normalize_username,
)
from backend.app.gateway.auth.password import hash_password, verify_password
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.auth.settings import AuthSettings
from backend.app.gateway.auth.store.base import (
    UserAlreadyExistsError,
    UserStore,
    conflict_field_error,
)

logger = logging.getLogger(__name__)

#: `admin_reset_password` 生成的随机口令长度（token_urlsafe 的字符数）。
RESET_PASSWORD_BYTES = 24

#: `last_used_at` 的写入节流（§2.15）：每请求写库不值得。
LAST_USED_THROTTLE_SECONDS = 60


class AuthService:
    def __init__(
        self,
        store: UserStore,
        *,
        clock: Callable[[], float] = time.time,
        limiter: LoginRateLimiter | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._limiter = limiter if limiter is not None else login_rate_limiter
        self._new_id = id_factory or (lambda: uuid4().hex)

    # --- 首次初始化（§2.7）---

    async def needs_setup(self) -> bool:
        """没有 admin 时服务**不**自动建号，只提示访问 /setup。"""
        return await self._store.active_admin_count() == 0

    async def initialize(
        self, *, username: str, password: str, settings: AuthSettings
    ) -> tuple[dict, str, int]:
        """只在零 admin 时可用。并发输家拿到 409 `admin_already_exists`。

        用户名形状、口令策略的错误在这里就是**具体**的
        （422 `invalid_username` / `password_too_short` / …）。
        """
        handle = normalize_username(username)
        password_hash = hash_password(password)
        try:
            user = await self._store.create_first_admin(
                user_id=self._new_id(), email=None, username=handle, password_hash=password_hash
            )
        except UserAlreadyExistsError as exc:
            raise conflict_field_error(exc, email=None, username=handle) from exc
        if user is None:
            # 不点名现有 admin 的用户名：这条路由是公开的。
            raise AdminAlreadyExists(
                "an admin account already exists; sign in instead of running setup again"
            )
        logger.info("initialized first admin id=%s", user["id"])
        token, expires_in = self.issue_token(user, settings=settings)
        return user, token, expires_in

    async def register(
        self, *, username: str, password: str, settings: AuthSettings
    ) -> tuple[dict, str, int]:
        if not settings.allow_registration:
            raise RegistrationClosed(
                "self-registration is disabled on this server; ask an admin to create the account"
            )
        handle = normalize_username(username)
        password_hash = hash_password(password)
        try:
            user = await self._store.create(
                user_id=self._new_id(),
                email=None,
                username=handle,
                password_hash=password_hash,
            )
        except UserAlreadyExistsError as exc:
            raise conflict_field_error(exc, email=None, username=handle) from exc
        token, expires_in = self.issue_token(user, settings=settings)
        return user, token, expires_in

    # --- 本地登录（§2.5 / §2.12）---

    async def login_local(
        self,
        *,
        username: str,
        password: str,
        peer_ip: str,
        settings: AuthSettings,
    ) -> tuple[dict, str, int]:
        """`username + password` 登录。

        失败按 **TCP peer IP** 计数，并且**说清是哪一种失败**。
        默认（`auth.local.specific_errors=true`）分四种回答：
        没这个账号 / 密码不对 / 账号被禁用 / 账号处于回收期。
        `false` 时全部收敛成 401 `invalid_credentials`，减少账号枚举面。

        禁用与回收期只在**口令正确**之后才说——不知道口令的人不该从响应里读出
        "这个账号存在且被禁用了"。
        """
        if (remaining := self._limiter.locked_for(peer_ip)) > 0:
            seconds = int(remaining) + 1
            raise LoginRateLimited(
                f"too many failed login attempts from this address; try again in {seconds} seconds",
                retry_after=seconds,
            )

        # 用户名格式错就报 invalid_username，不要伪装成"密码不对"。
        lookup = normalize_username(username)
        user = await self._store.get_by_username(lookup)
        if user is None:
            self._record_failure(peer_ip, settings)
            raise self._login_failure(
                settings,
                AccountNotFound(f"no account for username {lookup!r}; register first"),
            )
        if not verify_password(password, user["password_hash"]):
            self._record_failure(peer_ip, settings)
            raise self._login_failure(settings, WrongPassword("password does not match"))

        # 到这儿说明口令是对的——后面两个理由可以说，而且不算失败尝试。
        self._limiter.reset(peer_ip)
        if user["disabled"]:
            raise self._login_failure(
                settings,
                AccountDisabled(
                    f"account {user['username']!r} is disabled; ask an admin to re-enable it"
                ),
            )
        if user["deleted_at"] is not None:
            raise self._login_failure(
                settings,
                AccountDeleted(
                    f"account {user['username']!r} is pending deletion; an admin can restore it "
                    "during the retention window"
                ),
            )

        token, expires_in = self.issue_token(user, settings=settings)
        return user, token, expires_in

    @staticmethod
    def _login_failure(settings: AuthSettings, specific: CodedError) -> CodedError:
        """`specific_errors=false` 时把具体理由收敛成同一个 401（防账号枚举）。"""
        if settings.specific_errors:
            return specific
        return InvalidCredentials("invalid username or password")

    def _record_failure(self, peer_ip: str, settings: AuthSettings) -> None:
        self._limiter.record_failure(
            peer_ip, max_attempts=settings.max_login_attempts, lockout_seconds=settings.lockout_seconds
        )

    def issue_token(self, user: dict, *, settings: AuthSettings) -> tuple[str, int]:
        return encode_access_token(
            user_id=user["id"],
            token_version=int(user["token_version"]),
            secret=settings.jwt_secret,
            ttl_seconds=settings.jwt_ttl_seconds,
            now=int(self._clock()),
        )

    # --- 每次请求的校验（§2.4 / §2.14）---

    async def authenticate_session(self, token: str, *, settings: AuthSettings) -> AuthResult | None:
        """签名 + exp + 用户存在 + `tv` 匹配 + 未禁用/未删除。

        `disabled` **每次鉴权都查库**：只在签发时查的话，禁用要等 token 过期才生效，
        而那个窗口在事故处置时不可接受（§2.14）。
        """
        from backend.app.gateway.auth.jwt import decode_access_token

        claims = decode_access_token(token, secret=settings.jwt_secret, now=int(self._clock()))
        if claims is None:
            return None
        user = await self._store.get(claims.subject)
        if user is None or user["deleted_at"] is not None or user["disabled"]:
            return None
        if int(user["token_version"]) != claims.token_version:
            return None
        return self._result_for(user, kind=AuthKind.session)

    async def authenticate_pat(self, token: str, *, settings: AuthSettings) -> AuthResult | None:
        """Bearer PAT。**绝不回退到 cookie**（§2.6）：无效就是硬 401。"""
        if not token.startswith(PAT_PREFIX):
            return None
        row = await self._store.get_pat_by_hash(_token_digest(token))
        if row is None:
            return None
        if _parse_instant(row["expires_at"]) <= datetime.now(UTC):
            return None  # 到期即 401，不给宽限
        user = await self._store.get(row["user_id"])
        if user is None or user["deleted_at"] is not None or user["disabled"]:
            return None
        if int(row.get("token_version") or 0) != int(user["token_version"]):
            # PAT 与 session **共享** `token_version`：改密 / 重置 / 软删把旧 PAT 一起废掉（§2.14）。
            return None
        await self._touch_last_used(row)
        scopes = frozenset(row["scopes"] or ()) - {"admin"}  # 永不带 admin（§2.15）
        return AuthResult(
            kind=AuthKind.pat,
            user_id=make_safe_user_id(user["id"]),
            identity=user["id"],
            role=None,  # 角色能力对 PAT 一律关闭
            scopes=scopes,
            needs_setup=False,
        )

    async def _touch_last_used(self, row: dict) -> None:
        now = datetime.now(UTC)
        previous = row.get("last_used_at")
        if previous is not None and (now - _parse_instant(previous)).total_seconds() < LAST_USED_THROTTLE_SECONDS:
            return
        await self._store.touch_pat(row["id"], when=now)

    def _result_for(self, user: dict, *, kind: AuthKind) -> AuthResult:
        return AuthResult(
            kind=kind,
            user_id=make_safe_user_id(user["id"]),
            identity=user["id"],
            role=user["system_role"],
            scopes=None,
            needs_setup=bool(user["needs_setup"]),
        )

    # --- 本人操作（§2.13）---

    async def change_password(
        self, *, user_id: str, current_password: str, new_password: str
    ) -> dict:
        """改密 = 撤销**所有**会话（`token_version += 1`）。自己这枚也失效。"""
        user = await self._require_user(user_id)
        if not verify_password(current_password, user["password_hash"]):
            raise InvalidCredentials("current password is incorrect")
        await self._store.update(user_id, password_hash=hash_password(new_password), needs_setup=False)
        bumped = await self._store.bump_token_version(user_id)
        logger.info("password changed user_id=%s", user_id)
        return bumped or user

    # --- admin 管理（§2.13 / §2.16）---

    async def admin_reset_password(
        self, *, actor_id: str, user_id: str, allow_self: bool = False
    ) -> str:
        """随机口令 + `needs_setup=true`：用户下次登录只能改密。返回明文**仅**给 CLI/调用方。

        `allow_self=True` 只给 `reset_admin` 这条离线救援路径用：它没有"另一个人"可当 actor。
        """
        if actor_id == user_id and not allow_self:
            raise CannotModifySelf("use change-password for your own account")
        await self._require_user(user_id)
        password = secrets.token_urlsafe(RESET_PASSWORD_BYTES)  # noqa: S105 —— 一次性随机口令
        await self._store.update(user_id, password_hash=hash_password(password), needs_setup=True)
        await self._store.bump_token_version(user_id)
        logger.info("password reset actor_id=%s user_id=%s", actor_id, user_id)
        return password

    async def set_disabled(self, *, actor_id: str, user_id: str, disabled: bool) -> dict:
        if actor_id == user_id:
            raise CannotModifySelf("cannot disable your own account")
        user = await self._require_user(user_id)
        if disabled and user["system_role"] == "admin" and await self._store.active_admin_count() <= 1:
            raise LastAdmin("cannot disable the last admin")
        updated = await self._store.update(user_id, disabled=bool(disabled))
        if updated is None:
            raise UserNotFound(f"user {user_id} not found")
        return updated

    async def delete_user(self, *, actor_id: str, user_id: str) -> None:
        """软删：行保留、令牌全失效、桶**先标记后删**。回收期内 admin 可恢复。"""
        if actor_id == user_id:
            raise CannotModifySelf("cannot delete your own account")
        await self._require_user(user_id)
        stamp = datetime.now(UTC)
        if not await self._store.conditional_soft_delete(user_id, deleted_at=stamp):
            if await self._store.get(user_id) is None:
                raise UserNotFound(f"user {user_id} not found")
            raise LastAdmin("cannot delete the last admin")
        await self._store.bump_token_version(user_id)
        mark_user_bucket_deleted(user_id, deleted_at=iso_utc(stamp) or "")
        logger.info("user soft-deleted actor_id=%s user_id=%s", actor_id, user_id)

    async def restore_user(self, *, actor_id: str, user_id: str) -> dict:
        del actor_id
        user = await self._require_user(user_id)
        updated = await self._store.update(user_id, deleted_at=None, disabled=False)
        if updated is None:
            raise UserNotFound(f"user {user_id} not found")
        unmark_user_bucket(user["id"])
        return updated

    async def set_role(self, *, actor_id: str, user_id: str, role: str) -> dict:
        if role not in SYSTEM_ROLES:
            raise ValueError(f"unknown role {role!r}; M9 only knows {sorted(SYSTEM_ROLES)}")
        user = await self._require_user(user_id)
        if user["system_role"] == role:
            return user
        if role != "admin":
            # 并发降级两个 admin 靠条件写，不能先查再写（§2.13 不变量 1）。
            if not await self._store.conditional_demote(user_id):
                raise LastAdmin("cannot demote the last admin")
            return await self._require_user(user_id)
        updated = await self._store.update(user_id, system_role="admin")
        if updated is None:
            raise UserNotFound(f"user {user_id} not found")
        logger.info("role changed actor_id=%s user_id=%s role=%s", actor_id, user_id, role)
        return updated

    async def set_email(self, *, actor_id: str, user_id: str, email: str) -> dict:
        """改邮箱 **不换桶**：`users.id` 是 uuid，桶名派生自它（§2.2）。"""
        del actor_id
        await self._require_user(user_id)
        normalized = normalize_email(email)
        try:
            updated = await self._store.update(user_id, email=normalized)
        except UserAlreadyExistsError as exc:
            raise conflict_field_error(exc, email=normalized, username=None) from exc
        if updated is None:
            raise UserNotFound(f"user {user_id} not found")
        return updated

    async def set_username(self, *, actor_id: str, user_id: str, username: str) -> dict:
        """改用户名 **也不换桶**——和改邮箱是同一条规则（§2.2）。

        这条比改邮箱更容易写错：用户名看起来"本来就是 id"，顺手拿它拼路径很方便。
        桶名只来自 `users.id`，改名之后用户看到的还是同一批对话。
        """
        del actor_id
        await self._require_user(user_id)
        handle = normalize_username(username)
        try:
            updated = await self._store.update(user_id, username=handle)
        except UserAlreadyExistsError as exc:
            raise conflict_field_error(exc, email=None, username=handle) from exc
        if updated is None:
            raise UserNotFound(f"user {user_id} not found")
        logger.info("username changed user_id=%s username=%s", user_id, handle)
        return updated

    async def list_users(self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False) -> list[dict]:
        return await self._store.list_users(limit=limit, offset=offset, include_deleted=include_deleted)

    async def get_user_by_storage_id(self, storage_id: str) -> dict | None:
        """内部通道的 owner 头是**存储 ID**，不是主键（§2.9 关 2）。"""
        return await self._store.get_by_storage_id(storage_id)

    async def require_user(self, user_id: str) -> dict:
        """查不到 → 404 `user_not_found`。路由与 CLI 都走它，不各自拼错误。"""
        return await self._require_user(user_id)

    async def _require_user(self, user_id: str) -> dict:
        user = await self._store.get(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id} not found")
        return user

    # --- PAT（§2.15）---

    async def create_pat(
        self,
        *,
        user_id: str,
        name: str,
        scopes: list[str] | tuple[str, ...],
        days: int,
        settings: AuthSettings,
    ) -> tuple[str, dict]:
        """明文只在**这个返回值**里出现一次；之后任何接口都不再返回。"""
        self._validate_scopes(scopes)
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > settings.pat_max_days:
            raise ValueError(f"expires_in_days must be between 1 and {settings.pat_max_days}")
        owner = await self._require_user(user_id)
        if await self._store.count_pats(user_id) >= settings.max_pats:
            raise TooManyPats(f"at most {settings.max_pats} tokens per user; revoke an old one first")

        plain = PAT_PREFIX + secrets.token_urlsafe(32)
        row = await self._store.create_pat(
            pat_id=self._new_id(),
            user_id=user_id,
            name=name,
            token_hash=_token_digest(plain),
            token_prefix=plain[:PAT_DISPLAY_PREFIX_CHARS],
            token_version=int(owner["token_version"]),
            scopes=sorted(set(scopes)),
            expires_at=datetime.now(UTC) + timedelta(days=days),
        )
        logger.info("pat created user_id=%s pat_id=%s scopes=%s", user_id, row["id"], sorted(set(scopes)))
        return plain, row

    @staticmethod
    def _validate_scopes(scopes: list[str] | tuple[str, ...]) -> None:
        if not scopes:
            raise InvalidScope("at least one scope is required")
        unknown = sorted(set(scopes) - PAT_SCOPE_MENU)
        if unknown:
            # "给 admin 发 PAT" 不实现：admin 不在菜单里，写了就是 unknown。
            raise InvalidScope(f"unknown scope(s): {', '.join(unknown)}")

    async def list_pats(self, user_id: str) -> list[dict]:
        rows = await self._store.list_pats(user_id)
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "scopes": list(row["scopes"] or ()),
                "expires_at": row["expires_at"],
                "last_used_at": row["last_used_at"],
                "display": f"{row['token_prefix']}\u2026",
            }
            for row in rows
        ]

    async def revoke_pat(self, *, user_id: str, pat_id: str) -> bool:
        """删行，不是标记；只能吊销自己的。"""
        revoked = await self._store.delete_pat(pat_id, user_id=user_id)
        if revoked:
            logger.info("pat revoked user_id=%s pat_id=%s", user_id, pat_id)
        return revoked


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_instant(value: Any) -> datetime:
    """存储层返回 UTC ISO（`...Z`）或 datetime。两种都接住，比较一律在 UTC。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
