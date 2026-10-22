"""§4.1 threads_meta projection: columns, reserved metadata keys, SQLite locks."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import event, inspect, text

from agentkit.persistence import engine as engine_mod
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.schema import ThreadMetaRow
from agentkit.persistence.thread_meta.sql import strip_reserved_metadata


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def test_thread_meta_columns_match_the_projection() -> None:
    table = ThreadMetaRow.__table__
    columns = {column.name: column for column in table.columns}
    assert set(columns) == {
        "thread_id",
        "incarnation",
        "assistant_id",
        "user_id",
        "display_name",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
    }
    assert columns["thread_id"].primary_key
    assert columns["thread_id"].type.length == 64
    assert columns["incarnation"].nullable and columns["incarnation"].type.length == 32
    assert columns["assistant_id"].nullable and columns["assistant_id"].type.length == 128
    assert columns["user_id"].nullable and columns["user_id"].type.length == 64
    assert columns["display_name"].nullable and columns["display_name"].type.length == 256
    assert columns["status"].nullable is False
    assert columns["status"].default.arg == "idle"
    assert columns["metadata_json"].nullable is False
    assert columns["metadata_json"].default.is_callable
    assert columns["metadata_json"].default.arg.__wrapped__ is dict
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True
    assert columns["updated_at"].onupdate is not None
    indexed = {tuple(index.columns.keys()) for index in table.indexes}
    assert ("assistant_id",) in indexed
    assert ("user_id",) in indexed


def test_strip_reserved_metadata_drops_server_keys_without_error() -> None:
    original = {
        "owner_id": "hidden",
        "user_id": "hidden",
        "myflow_trace_id": "trace",
        "topic": "中文",
    }
    assert strip_reserved_metadata(original) == {"topic": "中文"}
    assert original["user_id"] == "hidden"
    assert strip_reserved_metadata(None) == {}
    assert strip_reserved_metadata({}) == {}


def test_empty_sqlite_bootstrap_creates_threads_meta(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            def _schema(sync_session):
                inspector = inspect(sync_session.connection())
                return (
                    inspector.get_columns("threads_meta"),
                    inspector.get_indexes("threads_meta"),
                    inspector.get_pk_constraint("threads_meta"),
                )

            columns, indexes, pk = await session.run_sync(_schema)
        by_name = {column["name"]: column for column in columns}
        assert by_name["thread_id"]["nullable"] is False
        assert by_name["incarnation"]["nullable"] is True
        assert by_name["user_id"]["nullable"] is True
        assert by_name["status"]["nullable"] is False
        assert by_name["metadata_json"]["nullable"] is False
        assert pk["constrained_columns"] == ["thread_id"]
        indexed = {tuple(index["column_names"]) for index in indexes}
        assert ("assistant_id",) in indexed
        assert ("user_id",) in indexed

        async with factory() as session:
            session.add(ThreadMetaRow(thread_id="t1"))
            await session.commit()
        async with factory() as session:
            row = await session.get(ThreadMetaRow, "t1")
            assert row is not None
            assert row.status == "idle"
            assert row.metadata_json == {}
            assert row.incarnation is None
            assert row.user_id is None
            assert row.created_at is not None
            assert row.updated_at is not None

    _run(scenario)


def test_sqlite_transactions_begin_immediate(tmp_path: Path) -> None:
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("BEGIN"):
            statements.append(statement)

    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        engine = engine_mod._engine
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            factory = get_session_factory()
            assert factory is not None
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            assert any("BEGIN IMMEDIATE" in statement.upper() for statement in statements)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    _run(scenario)
