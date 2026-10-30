"""SQLAlchemy-backed session state and message dedup repositories."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentkit.agents.memory.openviking.contracts import CapturedMessage
from agentkit.agents.memory.openviking.models import (
    MemoryCaptureDedupRow,
    MemorySyncStateRow,
)


def _now() -> datetime:
    return datetime.now(UTC)


class MemorySyncStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: str, thread_id: str) -> MemorySyncStateRow | None:
        async with self._session_factory() as session:
            return await session.get(MemorySyncStateRow, (user_id, thread_id))

    async def get_or_create(
        self,
        *,
        user_id: str,
        thread_id: str,
        ov_user: str,
        ov_session_id: str,
        session_incarnation: str,
        stream_shard: int,
    ) -> MemorySyncStateRow:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is not None:
                return row
            now = _now()
            values = {
                "user_id": user_id,
                "thread_id": thread_id,
                "ov_user": ov_user,
                "ov_session_id": ov_session_id,
                "session_incarnation": session_incarnation,
                "session_epoch": 1,
                "stream_shard": stream_shard,
                "session_started_at": now,
                "state": "idle",
                "attempts": 0,
                "updated_at": now,
            }
            await _insert_ignore(
                session,
                MemorySyncStateRow,
                values,
                index_elements=["user_id", "thread_id"],
            )
            await session.commit()
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:  # pragma: no cover - defensive, insert is atomic
                raise RuntimeError("sync state disappeared during creation")
            return row

    async def bump_epoch(self, user_id: str, thread_id: str) -> MemorySyncStateRow:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:
                raise KeyError(f"missing sync state for {user_id}/{thread_id}")
            row.session_epoch += 1
            row.state = "idle"
            await session.commit()
            await session.refresh(row)
            return row

    async def record_append(
        self,
        user_id: str,
        thread_id: str,
        *,
        message_id: str,
        stream_id: str,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:
                return
            now = _now()
            row.last_message_at = now
            row.last_append_at = now
            row.last_stream_id = stream_id or row.last_stream_id
            row.last_message_id = message_id
            await session.commit()

    async def mark_committed(
        self, user_id: str, thread_id: str, *, reason: str = ""
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:
                return
            now = _now()
            row.last_commit_at = now
            row.last_commit_reason = reason[:128]
            row.state = "done"
            row.last_error = None
            await session.commit()

    async def list_active(
        self, *, user_id: str | None = None, limit: int = 500
    ) -> list[MemorySyncStateRow]:
        async with self._session_factory() as session:
            statement = select(MemorySyncStateRow).where(
                MemorySyncStateRow.state.not_in(("deleted", "done"))
            )
            if user_id is not None:
                statement = statement.where(MemorySyncStateRow.user_id == user_id)
            statement = statement.order_by(MemorySyncStateRow.updated_at).limit(limit)
            return list((await session.scalars(statement)).all())

    async def set_state(self, user_id: str, thread_id: str, state: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:
                return
            row.state = state
            await session.commit()

    async def mark_deleted(self, user_id: str, thread_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemorySyncStateRow, (user_id, thread_id))
            if row is None:
                return
            row.state = "deleted"
            row.deleted_at = _now()
            await session.commit()


class CaptureDedupStore:
    """At-least-once dedup state for one captured message.

    A row is a terminal success marker only after appended_at is set.
    Startup reconciliation must not scan normal appended rows.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _key(captured: CapturedMessage) -> tuple[str, str, int, str, str]:
        return (
            captured.user_id,
            captured.thread_id,
            captured.session_epoch,
            captured.session_incarnation,
            captured.message_id,
        )

    async def reserve(self, captured: CapturedMessage) -> str:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            if row is not None:
                # An appended row without appended_at is the append-success /
                # DB-failure anomaly. It is not terminal; let the worker retry
                # it instead of acking a message that has no success marker.
                if row.state == "appended" and row.appended_at is None:
                    return "pending"
                return row.state
            await _insert_ignore(
                session,
                MemoryCaptureDedupRow,
                {
                    "user_id": captured.user_id,
                    "thread_id": captured.thread_id,
                    "session_epoch": captured.session_epoch,
                    "session_incarnation": captured.session_incarnation,
                    "message_id": captured.message_id,
                    "turn_id": captured.turn_id,
                    "sequence": captured.sequence,
                    "content_sha256": _content_sha256(captured),
                    "state": "pending",
                    "attempts": 0,
                    "created_at": _now(),
                },
                index_elements=[
                    "user_id",
                    "thread_id",
                    "session_epoch",
                    "session_incarnation",
                    "message_id",
                ],
            )
            await session.commit()
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            return "pending" if row is None else row.state

    async def state(self, captured: CapturedMessage) -> str:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            return "missing" if row is None else row.state

    async def mark_appending(self, captured: CapturedMessage, *, lease_seconds: int) -> bool:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            if row is None or (row.state == "appended" and row.appended_at is not None):
                return False
            now = _now()
            if row.state == "appending" and row.lease_until is not None:
                if _aware(row.lease_until) > now:
                    return False
            row.state = "appending"
            row.appending_at = now
            row.lease_owner = "memory-worker"
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.attempts += 1
            await session.commit()
            return True

    async def mark_appended(
        self,
        captured: CapturedMessage,
        *,
        ov_message_ids: list[str] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            if row is None:
                return
            row.state = "appended"
            row.appended_at = _now()
            row.ov_message_ids = json.dumps(ov_message_ids or [], ensure_ascii=False)
            row.lease_until = None
            await session.commit()

    async def uncertain(self, *, limit: int = 100) -> list[MemoryCaptureDedupRow]:
        async with self._session_factory() as session:
            statement = (
                select(MemoryCaptureDedupRow)
                .where(
                    or_(
                        MemoryCaptureDedupRow.state.in_(("pending", "appending")),
                        (
                            (MemoryCaptureDedupRow.state == "appended")
                            & (MemoryCaptureDedupRow.appended_at.is_(None))
                        ),
                    )
                )
                .order_by(MemoryCaptureDedupRow.created_at)
                .limit(limit)
            )
            return list((await session.scalars(statement)).all())


async def _insert_ignore(
    session: AsyncSession,
    table: Any,
    values: dict[str, Any],
    *,
    index_elements: list[str],
) -> None:
    """Run an idempotent INSERT for SQLite/PostgreSQL.

    The first capture for a thread (and the first profile read) can happen from
    concurrent requests. A plain SELECT-then-INSERT turns that into a unique
    constraint error; ON CONFLICT DO NOTHING makes the loser re-read the row.
    """

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:  # pragma: no cover - supported deployments are SQLite/PostgreSQL
        dialect_insert = insert
    statement = dialect_insert(table).values(**values)
    if dialect in {"sqlite", "postgresql"}:
        statement = statement.on_conflict_do_nothing(index_elements=index_elements)
    await session.execute(statement)


def _content_sha256(captured: CapturedMessage) -> str:
    payload = json.dumps(captured.message, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
