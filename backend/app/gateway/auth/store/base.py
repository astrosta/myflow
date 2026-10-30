"""账号与 PAT 的仓储接口。调用方依赖这个类型，不依赖后端。"""

from __future__ import annotations

import abc
from typing import Any

#: `update()` 允许写的列。白名单，避免路由传进来一个任意键就改库。
UPDATABLE_FIELDS = frozenset(
    {
        "email",
        "username",
        "password_hash",
        "system_role",
        "needs_setup",
        "disabled",
        "deleted_at",
        "token_version",
    }
)


class UserAlreadyExistsError(Exception):
    """`email` / `username` / `id` 已经存在。

    `field` 说明撞的是哪一个：邮箱要换邮箱、用户名要换用户名，
    两种情况的下一步完全不一样（§2.12 的错误表把它们分成两个 code）。
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def conflict_field_error(exc: UserAlreadyExistsError, *, email: str | None, username: str | None):
    """把仓储的"撞车"翻译成**具体**的 409。

    仓储知道撞的是哪一列（`field`），路由层不该去猜。认不出来时退回通用的
    `user_already_exists`——宁可少说一句，也不要指错字段让用户白改一遍。
    """
    from backend.app.gateway.auth.errors import UsernameTaken, UserAlreadyExists

    if exc.field == "username" and username is not None:
        return UsernameTaken(f"username {username!r} is taken; pick another one")
    if email is not None:
        return UserAlreadyExists(f"email {email!r} is already registered; sign in instead")
    return UserAlreadyExists(str(exc) or "account already exists")


class UserStore(abc.ABC):
    @abc.abstractmethod
    async def get(self, user_id: str) -> dict | None: ...

    @abc.abstractmethod
    async def get_by_email(self, email: str) -> dict | None:
        """Legacy/profile lookup only; login never uses email."""

    @abc.abstractmethod
    async def get_by_username(self, username: str) -> dict | None: ...

    @abc.abstractmethod
    async def get_by_identifier(self, identifier: str) -> dict | None:
        """登录用：邮箱**或**用户名。两个命名空间的字符集不重叠（用户名不许 `@`），
        所以调用方靠"有没有 @"就能决定走哪条索引。"""

    @abc.abstractmethod
    async def get_by_storage_id(self, storage_id: str) -> dict | None:
        """按**存储 ID**回查账号（内部通道的 owner 头走这条路径，§2.9）。

        存储 ID 是 `make_safe_user_id(users.id)` 派生的，不保证等于主键，
        所以先按主键命中，未命中再扫一遍（账号表很小，M9 接受）。
        """

    @abc.abstractmethod
    async def create(
        self,
        *,
        user_id: str,
        email: str | None,
        username: str,
        password_hash: str | None,
        system_role: str = "user",
        needs_setup: bool = False,
    ) -> dict: ...

    @abc.abstractmethod
    async def create_first_admin(
        self, *, user_id: str, email: str | None, username: str, password_hash: str | None
    ) -> dict | None:
        """**只在零 admin 时**插入。返回 None = 已经有 admin（并发调用者的 409）。"""

    @abc.abstractmethod
    async def list_users(
        self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False
    ) -> list[dict]: ...

    @abc.abstractmethod
    async def update(self, user_id: str, **fields: Any) -> dict | None: ...

    @abc.abstractmethod
    async def bump_token_version(self, user_id: str) -> dict | None:
        """`token_version += 1` 并返回新行。改密 / 重置的**唯一**撤销机制（§2.14）。"""

    @abc.abstractmethod
    async def active_admin_count(self) -> int: ...

    @abc.abstractmethod
    async def first_admin_id(self) -> str | None:
        """迁移脚本用：旧布局的 thread 归第一个 admin（§2.8 规则 2）。"""

    @abc.abstractmethod
    async def conditional_demote(self, user_id: str) -> bool:
        """仅当**还有别的** admin 时才降级。返回 False = 对方是最后一个 admin。"""

    @abc.abstractmethod
    async def conditional_soft_delete(self, user_id: str, *, deleted_at: Any) -> bool:
        """软删。最后一个 admin 删不掉（返回 False）。"""

    # --- PAT（§2.15）---

    @abc.abstractmethod
    async def create_pat(
        self,
        *,
        pat_id: str,
        user_id: str,
        name: str,
        token_hash: str,
        token_prefix: str,
        token_version: int,
        scopes: list[str],
        expires_at: Any,
    ) -> dict: ...

    @abc.abstractmethod
    async def get_pat_by_hash(self, token_hash: str) -> dict | None: ...

    @abc.abstractmethod
    async def list_pats(self, user_id: str) -> list[dict]: ...

    @abc.abstractmethod
    async def count_pats(self, user_id: str) -> int: ...

    @abc.abstractmethod
    async def touch_pat(self, pat_id: str, *, when: Any) -> None: ...

    @abc.abstractmethod
    async def delete_pat(self, pat_id: str, *, user_id: str) -> bool: ...
