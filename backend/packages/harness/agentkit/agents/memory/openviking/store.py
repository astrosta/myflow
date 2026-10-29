"""SQLAlchemy-backed session state and message dedup repositories."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
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
            row = MemorySyncStateRow(
                user_id=user_id,
                thread_id=thread_id,
                ov_user=ov_user,
                ov_session_id=ov_session_id,
                session_incarnation=session_incarnation,
                session_epoch=1,
                stream_shard=stream_shard,
                session_started_at=_now(),
                state="idle",
                attempts=0,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

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
                return row.state
            row = MemoryCaptureDedupRow(
                user_id=captured.user_id,
                thread_id=captured.thread_id,
                session_epoch=captured.session_epoch,
                session_incarnation=captured.session_incarnation,
                message_id=captured.message_id,
                turn_id=captured.turn_id,
                sequence=captured.sequence,
                content_sha256=_content_sha256(captured),
                state="pending",
                attempts=0,
                created_at=_now(),
            )
            session.add(row)
            await session.commit()
            return "pending"

    async def state(self, captured: CapturedMessage) -> str:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            return "missing" if row is None else row.state

    async def mark_appending(self, captured: CapturedMessage, *, lease_seconds: int) -> bool:
        async with self._session_factory() as session:
            row = await session.get(MemoryCaptureDedupRow, self._key(captured))
            if row is None or row.state == "appended":
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


def _content_sha256(captured: CapturedMessage) -> str:
    payload = json.dumps(captured.message, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
