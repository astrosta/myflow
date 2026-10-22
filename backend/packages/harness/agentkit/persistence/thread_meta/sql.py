"""SQL thread metadata. Methods open a short session themselves."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.base import row_to_dict
from agentkit.persistence.json_compat import json_value_matches
from agentkit.persistence.thread_meta.base import (
    ThreadMetaStore,
    ThreadOwnershipConflictError,
    require_thread_owner,
    resolve_thread_user,
)
from agentkit.persistence.thread_meta.model import ThreadMetaRow
from agentkit.runtime.user_context import AUTO

_SERVER_RESERVED_METADATA_KEYS = frozenset({
    "owner_id",
    "user_id",
    "myflow_trace_id",
})


def strip_reserved_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """create 和 update_metadata 共用的唯一入口。请求体里出现保留键就丢掉，不报错。"""
    return {key: value for key, value in (metadata or {}).items() if key not in _SERVER_RESERVED_METADATA_KEYS}


class SqlThreadMetaStore(ThreadMetaStore):
    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def create(
        self,
        thread_id,
        *,
        assistant_id=None,
        user_id=AUTO,
        display_name=None,
        metadata=None,
    ) -> dict:
        owner = require_thread_owner(resolve_thread_user(user_id, "create"))
        async with self._sf() as session:
            existing = await self._by_owner(session, thread_id, None)
            if existing is not None:
                return _existing_or_conflict(existing, owner)
            row = ThreadMetaRow(
                thread_id=thread_id,
                incarnation=uuid4().hex,
                assistant_id=assistant_id,
                user_id=owner,
                display_name=display_name or None,
                status="idle",
                metadata_json=strip_reserved_metadata(metadata),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._by_owner(session, thread_id, None)
                if existing is None:
                    raise
                return _existing_or_conflict(existing, owner)
            return row_to_dict(row)

    async def get(self, thread_id, *, user_id=AUTO) -> dict | None:
        owner = resolve_thread_user(user_id, "get")
        async with self._sf() as session:
            row = await self._by_owner(session, thread_id, owner)
            return row_to_dict(row) if row is not None else None

    async def search(
        self,
        *,
        metadata=None,
        status=None,
        limit: int = 100,
        offset: int = 0,
        user_id=AUTO,
    ) -> list[dict]:
        owner = resolve_thread_user(user_id, "search")
        statement = select(ThreadMetaRow)
        if owner is not None:
            statement = statement.where(ThreadMetaRow.user_id == owner)
        if status is not None:
            statement = statement.where(ThreadMetaRow.status == status)
        for key, expected in (metadata or {}).items():
            statement = statement.where(json_value_matches(ThreadMetaRow.metadata_json, key, expected))
        statement = (
            statement.order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._sf() as session:
            rows = (await session.scalars(statement)).all()
        return [row_to_dict(row) for row in rows]

    async def search_page(
        self,
        *,
        user_id: str | None,
        metadata=None,
        status: str | None = None,
        limit: int = 51,
        cursor: str | None = None,
        order: str = "desc",
    ) -> list[dict]:
        from agentkit.persistence.base import decode_keyset_cursor, encode_keyset_cursor, iso_utc
        from sqlalchemy import tuple_

        owner = resolve_thread_user(user_id, "search_page")
        statement = select(ThreadMetaRow)
        if owner is not None:
            statement = statement.where(ThreadMetaRow.user_id == owner)
        if status is not None:
            statement = statement.where(ThreadMetaRow.status == status)
        for key, expected in (metadata or {}).items():
            statement = statement.where(json_value_matches(ThreadMetaRow.metadata_json, key, expected))
        if cursor is not None:
            updated_at, thread_id = decode_keyset_cursor(cursor, order=order)
            stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            key = tuple_(ThreadMetaRow.updated_at, ThreadMetaRow.thread_id)
            bound = (stamp, thread_id)
            statement = statement.where(key < bound if order == "desc" else key > bound)
        if order == "asc":
            statement = statement.order_by(ThreadMetaRow.updated_at.asc(), ThreadMetaRow.thread_id.asc())
        else:
            statement = statement.order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
        statement = statement.limit(limit)
        async with self._sf() as session:
            rows = (await session.scalars(statement)).all()
        page = []
        for row in rows:
            body = row_to_dict(row)
            body["cursor"] = encode_keyset_cursor(order, iso_utc(row.updated_at) or "", row.thread_id)
            page.append(body)
        return page

    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None:
        await self._write(
            thread_id,
            resolve_thread_user(user_id, "update_display_name"),
            display_name=display_name or None,
            updated_at=datetime.now(UTC),
        )

    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None:
        await self._write(
            thread_id,
            resolve_thread_user(user_id, "update_status"),
            status=status,
            updated_at=datetime.now(UTC),
        )

    async def update_metadata(self, thread_id, metadata, *, touch: bool = True, user_id=AUTO) -> None:
        owner = resolve_thread_user(user_id, "update_metadata")
        async with self._sf() as session:
            row = await self._by_owner(session, thread_id, owner)
            if row is None:
                return
            merged = {**(row.metadata_json or {}), **strip_reserved_metadata(metadata)}
            values: dict[str, Any] = {
                "metadata_json": merged,
                "updated_at": datetime.now(UTC) if touch else row.updated_at,
            }
            await session.execute(
                update(ThreadMetaRow.__table__)
                .where(ThreadMetaRow.__table__.c.thread_id == thread_id)
                .values(**values)
            )
            await session.commit()

    async def delete(self, thread_id, *, user_id=AUTO) -> None:
        owner = resolve_thread_user(user_id, "delete")
        statement = sa_delete(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id)
        if owner is not None:
            statement = statement.where(ThreadMetaRow.user_id == owner)
        async with self._sf() as session:
            await session.execute(statement)
            await session.commit()

    async def check_access(self, thread_id, user_id: str, *, require_existing: bool = False) -> bool:
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
        if row is None:
            return not require_existing
        return row.user_id == user_id

    async def _by_owner(self, session, thread_id: str, user_id: str | None) -> ThreadMetaRow | None:
        statement = select(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id)
        if user_id is not None:
            statement = statement.where(ThreadMetaRow.user_id == user_id)
        return await session.scalar(statement)

    async def _write(self, thread_id: str, user_id: str | None, **values: Any) -> None:
        table = ThreadMetaRow.__table__
        statement = update(table).where(table.c.thread_id == thread_id)
        if user_id is not None:
            statement = statement.where(table.c.user_id == user_id)
        async with self._sf() as session:
            await session.execute(statement.values(**values))
            await session.commit()


def _existing_or_conflict(row: ThreadMetaRow, owner: str) -> dict:
    if row.user_id != owner:
        raise ThreadOwnershipConflictError(f"thread {row.thread_id} is owned by another user")
    return row_to_dict(row)

