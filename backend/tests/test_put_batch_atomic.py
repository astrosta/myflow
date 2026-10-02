"""§5.4 put_batch is all-or-nothing on every backend."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.models.run_event import RunEventRow
from agentkit.runtime.events.catalog import LLM_HUMAN_INPUT
from agentkit.runtime.events.store import JsonlRunEventStore, MemoryRunEventStore
from agentkit.runtime.events.store.base import RunEventSeqConflict
from agentkit.runtime.events.store.db import DbRunEventStore
from agentkit.runtime.events.store.jsonl import os as jsonl_os


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def _event(run_id: str = "run-1", **extra: object) -> dict:
    event = {
        "thread_id": "thread-1",
        "run_id": run_id,
        "event_type": LLM_HUMAN_INPUT.event_type,
        "category": LLM_HUMAN_INPUT.category,
        "content": "ok",
        "metadata": {},
    }
    event.update(extra)
    return event


def test_memory_batch_failure_leaves_previous_rows_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        await store.put(_event(content="kept"))
        with pytest.raises(ValueError, match="event_type"):
            await store.put_batch([_event(content="new"), _event(event_type="")])
        rows = await store.list_events("thread-1", "run-1")
        assert [row["content"] for row in rows] == ["kept"]

    _run(scenario)


def test_jsonl_batch_failure_creates_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        store = JsonlRunEventStore(tmp_path)
        with pytest.raises(ValueError, match=r"\.\./x"):
            await store.put_batch([_event(), _event(run_id="../x")])
        assert list(tmp_path.rglob("*.jsonl")) == []

        def fail_replace(src: str, dst: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(jsonl_os, "replace", fail_replace)
        with pytest.raises(OSError, match="disk full"):
            await store.put_batch([_event(content="one"), _event(content="two")])
        assert list(tmp_path.rglob("*.jsonl")) == []
        assert list(tmp_path.rglob("*.tmp")) == []

    _run(scenario)


def test_db_unique_conflict_retries_then_rolls_the_batch_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        attempts = 0

        def wrapping():
            session = factory()

            async def boom() -> None:
                nonlocal attempts
                attempts += 1
                raise IntegrityError("INSERT", {}, Exception("UNIQUE"))

            session.commit = boom  # type: ignore[method-assign]
            return session

        store = DbRunEventStore(wrapping)
        with pytest.raises(RunEventSeqConflict):
            await store.put_batch([_event(content="one"), _event(content="two")])
        assert attempts == 3
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(RunEventRow))
        assert count == 0

    _run(scenario)


def test_successful_batch_returns_the_same_length_in_order() -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        rows = await store.put_batch([_event(content="a"), _event(content="b")])
        assert [row["content"] for row in rows] == ["a", "b"]
        assert [row["seq"] for row in rows] == [1, 2]

    _run(scenario)

