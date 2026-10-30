"""SQL 账号仓储。方法各自开一个短会话，和 thread_meta 一个形状。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import exists, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.base import row_to_dict
from agentkit.utils import make_safe_user_id
from backend.app.gateway.auth.models import PersonalAccessTokenRow, UserRow
from backend.app.gateway.auth.store.base import (
    UPDATABLE_FIELDS,
    UserAlreadyExistsError,
    UserStore,
)

_INSERT_COLUMNS = (
    "id",
    "email",
    "username",
    "password_hash",
    "system_role",
    "needs_setup",
    "disabled",
    "token_version",
    "created_at",
    "updated_at",
)


def _field_from_integrity_error(exc: IntegrityError) -> str | None:
    """尽力从 unique 约束的名字里认出撞的是哪一列。

    这是**兜底**：正常路径靠事务内的预查（见 `create()`），只有并发抢同一个名字时
    才会走到这里。认不出来就返回 None，让调用方退回通用的 `user_already_exists`。
    """
    message = str(getattr(exc, "orig", exc)).lower()
    if "username" in message:
        return "username"
    if "email" in message:
        return "email"
    return None


def _admin_count_statement():
    """独立可执行的 count（给 `session.scalar` 用）。"""
    return (
        select(func.count())
        .select_from(UserRow)
        .where(UserRow.system_role == "admin", UserRow.deleted_at.is_(None))
    )


def _admin_count_subquery():
    """塞进 WHERE 的标量子查询（`UPDATE ... WHERE (SELECT count(*) ...) > 1`）。

    两者**不能混用**：`session.scalar()` 收到 ScalarSelect 会直接抛
    `ArgumentError`——而内存后端没有这个问题，所以只有真跑 SQL 才看得出来。
    """
    return _admin_count_statement().scalar_subquery()


class SqlUserStore(UserStore):
    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def get(self, user_id: str) -> dict | None:
        async with self._sf() as session:
            row = await session.get(UserRow, user_id)
            return row_to_dict(row) if row is not None else None

    async def get_by_email(self, email: str) -> dict | None:
        async with self._sf() as session:
            row = await session.scalar(select(UserRow).where(UserRow.email == email))
            return row_to_dict(row) if row is not None else None

    async def get_by_username(self, username: str) -> dict | None:
        async with self._sf() as session:
            row = await session.scalar(select(UserRow).where(UserRow.username == username))
            return row_to_dict(row) if row is not None else None

    async def get_by_identifier(self, identifier: str) -> dict | None:
        """先按邮箱，再按用户名——两条都是 unique 索引，命中即定。"""
        async with self._sf() as session:
            row = await session.scalar(
                select(UserRow).where(
                    (UserRow.email == identifier) | (UserRow.username == identifier)
                )
            )
            return row_to_dict(row) if row is not None else None

    async def get_by_storage_id(self, storage_id: str) -> dict | None:
        async with self._sf() as session:
            row = await session.get(UserRow, storage_id)
            if row is not None:
                return row_to_dict(row)
            # 主键不是 uuid（OIDC 之类）时，存储 ID 与主键不同名——账号表很小，扫一遍。
            rows = (await session.scalars(select(UserRow))).all()
            for candidate in rows:
                if make_safe_user_id(candidate.id) == storage_id:
                    return row_to_dict(candidate)
            return None

    async def create(
        self,
        *,
        user_id: str,
        email: str | None,
        username: str,
        password_hash: str | None,
        system_role: str = "user",
        needs_setup: bool = False,
    ) -> dict:
        now = datetime.now(UTC)
        row = UserRow(
            id=user_id,
            email=email,
            username=username,
            password_hash=password_hash,
            system_role=system_role,
            needs_setup=needs_setup,
            disabled=False,
            token_version=0,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            # 先查再插：不是为了代替 unique 索引，而是为了**知道撞的是哪一列**——
            # 用户该改邮箱还是改用户名，是两件完全不同的事（两个 code）。
            collision = await self._collision(
                session, user_id=user_id, email=email, username=username
            )
            if collision is not None:
                raise collision
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                # 并发抢名字：索引挡住了，这里只能靠错误文本认字段。
                await session.rollback()
                raise UserAlreadyExistsError(
                    f"user {user_id!r}, email {email!r} or username {username!r} already exists",
                    field=_field_from_integrity_error(exc),
                ) from exc
            return row_to_dict(row)

    @staticmethod
    async def _collision(
        session, *, user_id: str, email: str | None, username: str
    ) -> UserAlreadyExistsError | None:
        if await session.get(UserRow, user_id) is not None:
            return UserAlreadyExistsError(f"user {user_id!r} already exists", field="id")
        if email is not None and await session.scalar(
            select(UserRow.id).where(UserRow.email == email)
        ) is not None:
            return UserAlreadyExistsError(f"email {email!r} already exists", field="email")
        if await session.scalar(select(UserRow.id).where(UserRow.username == username)) is not None:
            return UserAlreadyExistsError(f"username {username!r} is taken", field="username")
        return None

    async def create_first_admin(
        self, *, user_id: str, email: str | None, username: str, password_hash: str | None
    ) -> dict | None:
        """**条件插入**：`INSERT ... SELECT ... WHERE NOT EXISTS (admin)`。

        不能先查再插：两个并发 initialize 都会通过检查，然后库里有两个"第一个 admin"。
        """
        now = datetime.now(UTC)
        values = {
            "id": user_id,
            "email": email,
            "username": username,
            "password_hash": password_hash,
            "system_role": "admin",
            "needs_setup": False,
            "disabled": False,
            "token_version": 0,
            "created_at": now,
            "updated_at": now,
        }
        guard = ~exists(select(UserRow.id).where(UserRow.system_role == "admin", UserRow.deleted_at.is_(None)))
        statement = insert(UserRow).from_select(
            list(_INSERT_COLUMNS),
            select(*[literal(values[column]) for column in _INSERT_COLUMNS]).where(guard),
        )
        async with self._sf() as session:
            collision = await self._collision(
                session, user_id=user_id, email=email, username=username
            )
            if collision is not None:
                raise collision
            try:
                result = await session.execute(statement)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise UserAlreadyExistsError(
                    f"user {user_id!r}, email {email!r} or username {username!r} already exists",
                    field=_field_from_integrity_error(exc),
                ) from exc
            if not result.rowcount:
                return None
            row = await session.get(UserRow, user_id)
            return row_to_dict(row) if row is not None else None

    async def list_users(
        self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False
    ) -> list[dict]:
        statement = select(UserRow)
        if not include_deleted:
            statement = statement.where(UserRow.deleted_at.is_(None))
        statement = statement.order_by(UserRow.created_at.asc(), UserRow.id.asc()).limit(limit).offset(offset)
        async with self._sf() as session:
            rows = (await session.scalars(statement)).all()
            return [row_to_dict(row) for row in rows]

    async def update(self, user_id: str, **fields: Any) -> dict | None:
        unknown = fields.keys() - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"users has no such column(s): {sorted(unknown)}")
        if not fields:
            return await self.get(user_id)
        values = {**fields, "updated_at": datetime.now(UTC)}
        async with self._sf() as session:
            try:
                result = await session.execute(
                    update(UserRow.__table__).where(UserRow.__table__.c.id == user_id).values(**values)
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                # 改邮箱 / 改用户名撞车：映射成同一个域异常，路由层不用认识 IntegrityError。
                raise UserAlreadyExistsError(
                    "email or username is already taken",
                    field=_field_from_integrity_error(exc),
                ) from exc
            if not result.rowcount:
                return None
            row = await session.get(UserRow, user_id)
            return row_to_dict(row) if row is not None else None

    async def bump_token_version(self, user_id: str) -> dict | None:
        async with self._sf() as session:
            result = await session.execute(
                update(UserRow.__table__)
                .where(UserRow.__table__.c.id == user_id)
                .values(token_version=UserRow.__table__.c.token_version + 1, updated_at=datetime.now(UTC))
            )
            await session.commit()
            if not result.rowcount:
                return None
            row = await session.get(UserRow, user_id)
            return row_to_dict(row) if row is not None else None

    async def active_admin_count(self) -> int:
        async with self._sf() as session:
            return int(await session.scalar(_admin_count_statement()) or 0)

    async def first_admin_id(self) -> str | None:
        statement = (
            select(UserRow.id)
            .where(UserRow.system_role == "admin", UserRow.deleted_at.is_(None))
            .order_by(UserRow.created_at.asc(), UserRow.id.asc())
            .limit(1)
        )
        async with self._sf() as session:
            return await session.scalar(statement)

    async def conditional_demote(self, user_id: str) -> bool:
        """`UPDATE ... WHERE (SELECT count(*) FROM users WHERE admin) > 1`。

        先查再写是 TOCTOU：两个并发降级会同时通过检查，之后系统没有 admin，
        只能靠 CLI 救（§2.13 不变量 1）。
        """
        async with self._sf() as session:
            result = await session.execute(
                update(UserRow.__table__)
                .where(
                    UserRow.__table__.c.id == user_id,
                    UserRow.__table__.c.system_role == "admin",
                    UserRow.__table__.c.deleted_at.is_(None),
                    _admin_count_subquery() > 1,
                )
                .values(system_role="user", updated_at=datetime.now(UTC))
            )
            await session.commit()
            return bool(result.rowcount)

    async def conditional_soft_delete(self, user_id: str, *, deleted_at: Any) -> bool:
        stamp = deleted_at if isinstance(deleted_at, datetime) else datetime.fromisoformat(str(deleted_at))
        async with self._sf() as session:
            row = await session.get(UserRow, user_id)
            if row is None or row.deleted_at is not None:
                return False
            if row.system_role == "admin" and int(await session.scalar(_admin_count_statement()) or 0) <= 1:
                return False
            result = await session.execute(
                update(UserRow.__table__)
                .where(UserRow.__table__.c.id == user_id, UserRow.__table__.c.deleted_at.is_(None))
                .values(deleted_at=stamp, updated_at=datetime.now(UTC))
            )
            await session.commit()
            return bool(result.rowcount)

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
        row = PersonalAccessTokenRow(
            id=pat_id,
            user_id=user_id,
            name=name,
            token_hash=token_hash,
            token_prefix=token_prefix,
            token_version=int(token_version),
            scopes=list(scopes),
            expires_at=expires_at,
            last_used_at=None,
            created_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            return row_to_dict(row)

    async def get_pat_by_hash(self, token_hash: str) -> dict | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(PersonalAccessTokenRow).where(PersonalAccessTokenRow.token_hash == token_hash)
            )
            return row_to_dict(row) if row is not None else None

    async def list_pats(self, user_id: str) -> list[dict]:
        statement = (
            select(PersonalAccessTokenRow)
            .where(PersonalAccessTokenRow.user_id == user_id)
            .order_by(PersonalAccessTokenRow.created_at.desc(), PersonalAccessTokenRow.id.desc())
        )
        async with self._sf() as session:
            rows = (await session.scalars(statement)).all()
            return [row_to_dict(row) for row in rows]

    async def count_pats(self, user_id: str) -> int:
        statement = (
            select(func.count())
            .select_from(PersonalAccessTokenRow)
            .where(PersonalAccessTokenRow.user_id == user_id)
        )
        async with self._sf() as session:
            return int(await session.scalar(statement) or 0)

    async def touch_pat(self, pat_id: str, *, when: Any) -> None:
        async with self._sf() as session:
            await session.execute(
                update(PersonalAccessTokenRow.__table__)
                .where(PersonalAccessTokenRow.__table__.c.id == pat_id)
                .values(last_used_at=when)
            )
            await session.commit()

    async def delete_pat(self, pat_id: str, *, user_id: str) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_delete(PersonalAccessTokenRow).where(
                    PersonalAccessTokenRow.id == pat_id, PersonalAccessTokenRow.user_id == user_id
                )
            )
            await session.commit()
            return bool(result.rowcount)
