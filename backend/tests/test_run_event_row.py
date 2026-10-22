"""§4.3 run_events ledger: columns and the per-thread seq unique constraint."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.schema import RunEventRow
from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def test_run_event_columns_match_the_ledger() -> None:
    table = RunEventRow.__table__
    columns = {column.name: column for column in table.columns}
    assert set(columns) == {
        "id",
        "thread_id",
        "run_id",
        "user_id",
        "event_type",
        "category",
        "content",
        "event_metadata",
        "seq",
        "created_at",
    }
    assert columns["id"].primary_key
    assert columns["thread_id"].nullable is False and columns["thread_id"].type.length == 64
    assert columns["run_id"].nullable is False and columns["run_id"].type.length == 64
    assert columns["user_id"].nullable and columns["user_id"].index
    assert columns["event_type"].nullable is False
    assert columns["event_type"].type.length == RUN_EVENT_TYPE_MAX_LENGTH == 32
    assert columns["category"].nullable is False
    assert columns["category"].type.length == RUN_EVENT_CATEGORY_MAX_LENGTH == 16
    assert columns["content"].nullable is False
    assert columns["content"].default.arg == ""
    assert columns["event_metadata"].nullable is False
    assert columns["event_metadata"].default.arg.__wrapped__ is dict
    assert columns["seq"].nullable is False
    assert columns["created_at"].type.timezone is True

    unique = next(
        constraint
        for constraint in table.constraints
        if constraint.name == "uq_events_thread_seq"
    )
    assert tuple(unique.columns.keys()) == ("thread_id", "seq")
    indexes = {index.name: tuple(index.columns.keys()) for index in table.indexes}
    assert indexes["ix_events_thread_cat_seq"] == ("thread_id", "category", "seq")
    assert indexes["ix_events_run"] == ("thread_id", "run_id", "seq")


def test_seq_is_unique_per_thread_on_an_empty_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            session.add(
                RunEventRow(
                    thread_id="thread-1",
                    run_id="run-1",
                    event_type="run.start",
                    category="trace",
                    content="中文",
                    seq=1,
                )
            )
            await session.commit()
        async with factory() as session:
            session.add(
                RunEventRow(
                    thread_id="thread-1",
                    run_id="run-2",
                    event_type="run.end",
                    category="outputs",
                    seq=1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        async with factory() as session:
            session.add(
                RunEventRow(
                    thread_id="thread-2",
                    run_id="run-1",
                    event_type="llm.human.input",
                    category="message",
                    content="中文",
                    seq=1,
                )
            )
            await session.commit()
            stored = await session.get(RunEventRow, 1)
            assert stored is not None
            assert stored.content == "中文"

    _run(scenario)
