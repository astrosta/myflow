"""SQL ledger. The per-thread lock avoids needless unique-index retries; the index is the real guard."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.base import row_to_dict
from agentkit.persistence.events.base import RunEventSeqConflict, RunEventStore
from agentkit.persistence.events.codec import decode_content, encode_for_db, normalize_event, same_thread
from agentkit.persistence.events.model import RunEventRow

SEQ_RETRIES = 3


class DbRunEventStore(RunEventStore):
    def __init__(self, session_factory, *, max_trace_content: int | None = 10240) -> None:
        self._sf = session_factory
        self._max_trace_content = max_trace_content
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()
        return lock

    async def put(self, event: dict[str, Any]) -> None:
        await self.put_batch([event])

    async def put_batch(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        if not events:
            return []
        normalized = [normalize_event(event) for event in events]
        thread_id = same_thread(normalized)
        async with self._lock_for(thread_id):
            return await self._insert_retry(thread_id, normalized)

    async def put_if_absent(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = normalize_event(event)
        async with self._lock_for(normalized["thread_id"]):
            existing = await self._find(normalized)
            if existing is not None:
                return existing, False
            inserted = await self._insert_retry(normalized["thread_id"], [normalized])
            return inserted[0], True

    async def list_messages(
        self,
        thread_id: str,
        limit: int,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.category == "message",
        )
        if before_seq is not None:
            statement = statement.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            statement = statement.where(RunEventRow.seq > after_seq)
        newest_window = before_seq is not None or after_seq is None
        if newest_window:
            statement = statement.order_by(RunEventRow.seq.desc()).limit(limit)
        else:
            statement = statement.order_by(RunEventRow.seq.asc()).limit(limit)
        rows = await self._load(statement)
        if newest_window:
            rows.reverse()
        return rows

    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        event_types: Sequence[str] | None = None,
        after_seq: int | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.run_id == run_id,
        )
        if event_types is not None:
            statement = statement.where(RunEventRow.event_type.in_(tuple(event_types)))
        if after_seq is not None:
            statement = statement.where(RunEventRow.seq > after_seq)
        if task_id is not None:
            # M8 §14.3：metadata["task_id"] 是 provider 的 tool_call_id。JSON 路径过滤，
            # 走的是已有的 (thread_id, run_id, seq) 索引，不改 schema。
            statement = statement.where(RunEventRow.event_metadata["task_id"].as_string() == task_id)
        statement = statement.order_by(RunEventRow.seq.asc())
        return await self._load(statement)

    async def count_messages(self, thread_id: str) -> int:
        async with self._sf() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(RunEventRow)
                .where(
                    RunEventRow.thread_id == thread_id,
                    RunEventRow.category == "message",
                )
            )
        return int(count or 0)

    async def _insert_retry(self, thread_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for _ in range(SEQ_RETRIES):
            try:
                return await self._insert_with_seqs(thread_id, events)
            except IntegrityError:
                continue
        raise RunEventSeqConflict(f"could not allocate seq for thread {thread_id}")

    async def _insert_with_seqs(self, thread_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self._sf() as session:
            last = await session.scalar(
                select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
            )
            rows = [
                self._to_row(event, seq=(last or 0) + index + 1) for index, event in enumerate(events)
            ]
            session.add_all(rows)
            await session.commit()
            return [_public(row) for row in rows]

    def _to_row(self, event: dict[str, Any], seq: int) -> RunEventRow:
        content, metadata = encode_for_db(
            event["content"],
            event["metadata"],
            event["category"],
            self._max_trace_content,
        )
        return RunEventRow(
            thread_id=event["thread_id"],
            run_id=event["run_id"],
            user_id=event.get("user_id"),
            event_type=event["event_type"],
            category=event["category"],
            content=content,
            event_metadata=metadata,
            seq=seq,
        )

    async def _find(self, event: dict[str, Any]) -> dict[str, Any] | None:
        statement = (
            select(RunEventRow)
            .where(
                RunEventRow.thread_id == event["thread_id"],
                RunEventRow.run_id == event["run_id"],
                RunEventRow.event_type == event["event_type"],
            )
            .order_by(RunEventRow.seq.asc())
            .limit(1)
        )
        async with self._sf() as session:
            row = await session.scalar(statement)
            return _public(row) if row is not None else None

    async def _load(self, statement) -> list[dict[str, Any]]:
        async with self._sf() as session:
            rows = list((await session.scalars(statement)).all())
            return [_public(row) for row in rows]


def _public(row: RunEventRow) -> dict[str, Any]:
    record = row_to_dict(row)
    record["content"] = decode_content(record["content"], record["metadata"], record["category"])
    return record
