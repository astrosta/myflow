"""进程内账号仓储。`database.backend=memory` 与测试用。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agentkit.persistence.base import iso_utc
from agentkit.utils import make_safe_user_id
from backend.app.gateway.auth.store.base import (
    UPDATABLE_FIELDS,
    UserAlreadyExistsError,
    UserStore,
)

_USER_FIELDS = (
    "id",
    "email",
    "username",
    "password_hash",
    "system_role",
    "needs_setup",
    "disabled",
    "deleted_at",
    "token_version",
    "created_at",
    "updated_at",
)


class MemoryUserStore(UserStore):
    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._pats: dict[str, dict] = {}

    # --- users ---

    async def get(self, user_id: str) -> dict | None:
        row = self._users.get(user_id)
        return deepcopy(row) if row is not None else None

    async def get_by_email(self, email: str) -> dict | None:
        for row in self._users.values():
            if row["email"] == email:
                return deepcopy(row)
        return None

    async def get_by_username(self, username: str) -> dict | None:
        for row in self._users.values():
            if row["username"] == username:
                return deepcopy(row)
        return None

    async def get_by_identifier(self, identifier: str) -> dict | None:
        found = await self.get_by_email(identifier)
        return found if found is not None else await self.get_by_username(identifier)

    async def get_by_storage_id(self, storage_id: str) -> dict | None:
        row = self._users.get(storage_id)
        if row is not None:
            return deepcopy(row)
        for candidate in self._users.values():
            if make_safe_user_id(candidate["id"]) == storage_id:
                return deepcopy(candidate)
        return None

    async def create(
        self,
        *,
        user_id: str,
        email: str,
        username: str,
        password_hash: str | None,
        system_role: str = "user",
        needs_setup: bool = False,
    ) -> dict:
        if user_id in self._users:
            raise UserAlreadyExistsError(f"user {user_id!r} already exists", field="id")
        if any(row["email"] == email for row in self._users.values()):
            raise UserAlreadyExistsError(f"email {email!r} already exists", field="email")
        if any(row["username"] == username for row in self._users.values()):
            raise UserAlreadyExistsError(f"username {username!r} is taken", field="username")
        now = iso_utc(datetime.now(UTC))
        row = {
            "id": user_id,
            "email": email,
            "username": username,
            "password_hash": password_hash,
            "system_role": system_role,
            "needs_setup": needs_setup,
            "disabled": False,
            "deleted_at": None,
            "token_version": 0,
            "created_at": now,
            "updated_at": now,
        }
        self._users[user_id] = row
        return deepcopy(row)

    async def create_first_admin(
        self, *, user_id: str, email: str, username: str, password_hash: str | None
    ) -> dict | None:
        if await self.active_admin_count() > 0:
            return None
        return await self.create(
            user_id=user_id,
            email=email,
            username=username,
            password_hash=password_hash,
            system_role="admin",
        )

    async def list_users(
        self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False
    ) -> list[dict]:
        rows = [row for row in self._users.values() if include_deleted or row["deleted_at"] is None]
        rows.sort(key=lambda row: (row["created_at"], row["id"]))
        return [deepcopy(row) for row in rows[offset : offset + limit]]

    async def update(self, user_id: str, **fields: Any) -> dict | None:
        unknown = fields.keys() - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"users has no such column(s): {sorted(unknown)}")
        if (email := fields.get("email")) is not None:
            # 唯一性在仓储层兜底：SQL 后端靠 unique 索引，内存后端必须自己守，
            # 否则两个后端的语义会在"改邮箱撞车"这一格上分叉。
            if any(row["email"] == email and row["id"] != user_id for row in self._users.values()):
                raise UserAlreadyExistsError(f"email {email!r} already exists", field="email")
        if (handle := fields.get("username")) is not None:
            if any(
                row["username"] == handle and row["id"] != user_id for row in self._users.values()
            ):
                raise UserAlreadyExistsError(f"username {handle!r} is taken", field="username")
        row = self._users.get(user_id)
        if row is None:
            return None
        for key, value in fields.items():
            row[key] = value.isoformat() if isinstance(value, datetime) else value
        row["updated_at"] = iso_utc(datetime.now(UTC))
        return deepcopy(row)

    async def bump_token_version(self, user_id: str) -> dict | None:
        row = self._users.get(user_id)
        if row is None:
            return None
        row["token_version"] = int(row["token_version"]) + 1
        row["updated_at"] = iso_utc(datetime.now(UTC))
        return deepcopy(row)

    async def active_admin_count(self) -> int:
        return sum(
            1
            for row in self._users.values()
            if row["system_role"] == "admin" and row["deleted_at"] is None
        )

    async def first_admin_id(self) -> str | None:
        admins = sorted(
            (
                row
                for row in self._users.values()
                if row["system_role"] == "admin" and row["deleted_at"] is None
            ),
            key=lambda row: (row["created_at"], row["id"]),
        )
        return admins[0]["id"] if admins else None

    async def conditional_demote(self, user_id: str) -> bool:
        row = self._users.get(user_id)
        if row is None or row["system_role"] != "admin" or row["deleted_at"] is not None:
            return False
        others = [
            candidate
            for candidate in self._users.values()
            if candidate["id"] != user_id
            and candidate["system_role"] == "admin"
            and candidate["deleted_at"] is None
        ]
        if not others:
            return False
        row["system_role"] = "user"
        row["updated_at"] = iso_utc(datetime.now(UTC))
        return True

    async def conditional_soft_delete(self, user_id: str, *, deleted_at: Any) -> bool:
        row = self._users.get(user_id)
        if row is None or row["deleted_at"] is not None:
            return False
        if row["system_role"] == "admin" and await self.active_admin_count() <= 1:
            return False
        row["deleted_at"] = deleted_at.isoformat() if isinstance(deleted_at, datetime) else deleted_at
        row["updated_at"] = iso_utc(datetime.now(UTC))
        return True

    # --- PAT ---

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
    ) -> dict:
        row = {
            "id": pat_id,
            "user_id": user_id,
            "name": name,
            "token_hash": token_hash,
            "token_prefix": token_prefix,
            "token_version": int(token_version),
            "scopes": list(scopes),
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
            "last_used_at": None,
            "created_at": iso_utc(datetime.now(UTC)),
        }
        self._pats[pat_id] = row
        return deepcopy(row)

    async def get_pat_by_hash(self, token_hash: str) -> dict | None:
        for row in self._pats.values():
            if row["token_hash"] == token_hash:
                return deepcopy(row)
        return None

    async def list_pats(self, user_id: str) -> list[dict]:
        rows = [row for row in self._pats.values() if row["user_id"] == user_id]
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return [deepcopy(row) for row in rows]

    async def count_pats(self, user_id: str) -> int:
        return sum(1 for row in self._pats.values() if row["user_id"] == user_id)

    async def touch_pat(self, pat_id: str, *, when: Any) -> None:
        row = self._pats.get(pat_id)
        if row is not None:
            row["last_used_at"] = when.isoformat() if isinstance(when, datetime) else when

    async def delete_pat(self, pat_id: str, *, user_id: str) -> bool:
        row = self._pats.get(pat_id)
        if row is None or row["user_id"] != user_id:
            return False
        del self._pats[pat_id]
        return True
