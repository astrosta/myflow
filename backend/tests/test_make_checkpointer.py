"""§6.5 make_checkpointer owns its connection and calls setup before yield."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from agentkit.config.app_config import AppConfig
from agentkit.config.database_config import DatabaseConfig
from agentkit.runtime.checkpointer.async_provider import (
    POSTGRES_CHECKPOINT_HINT,
    delete_thread_checkpoints,
    make_checkpointer,
    translate_checkpoint_import_error,
)


class _State(TypedDict, total=False):
    messages: list


async def _step(state: _State) -> _State:
    return {"messages": [*state.get("messages", []), "step"]}


def _graph(saver):
    builder = StateGraph(_State)
    builder.add_node("step", _step)
    builder.add_edge(START, "step")
    builder.add_edge("step", END)
    return builder.compile(checkpointer=saver)


def test_memory_checkpointer_deletes_every_namespace_for_the_thread() -> None:
    async def scenario() -> None:
        config = AppConfig(database=DatabaseConfig(backend="memory"))
        async with make_checkpointer(config) as saver:
            graph = _graph(saver)
            runnable = {"configurable": {"thread_id": "thread-1"}}
            await graph.ainvoke({"messages": ["hi"]}, runnable)
            await delete_thread_checkpoints(saver, "thread-1")
            snapshot = await graph.aget_state(runnable)
            assert snapshot.values.get("messages") in (None, [])

    asyncio.run(scenario())


def test_sqlite_checkpointer_sets_wal_and_setup_before_yield(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path / "nested" / "data"))
        async with make_checkpointer(AppConfig(database=database)) as saver:
            assert saver.is_setup is True
            cursor = await saver.conn.execute("PRAGMA journal_mode;")
            mode = await cursor.fetchone()
            assert mode[0] == "wal"
            cursor = await saver.conn.execute("PRAGMA foreign_keys;")
            assert (await cursor.fetchone())[0] == 1
            cursor = await saver.conn.execute("PRAGMA busy_timeout;")
            assert (await cursor.fetchone())[0] == 30000
            graph = _graph(saver)
            await graph.ainvoke({"messages": ["hi"]}, {"configurable": {"thread_id": "thread-1"}})
            cursor = await saver.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
            )
            assert await cursor.fetchone() is not None

    asyncio.run(scenario())


def test_missing_postgres_extra_names_uv_sync() -> None:
    exc = ModuleNotFoundError("missing", name="langgraph.checkpoint.postgres")
    with pytest.raises(ImportError, match="uv sync --extra postgres") as raised:
        translate_checkpoint_import_error(exc)
    assert "database.backend is postgres" in str(raised.value)
    assert POSTGRES_CHECKPOINT_HINT in str(raised.value)


def test_postgres_pool_is_not_the_orm_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class _Pool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, conninfo, *, kwargs, check) -> None:
            seen["conninfo"] = conninfo
            seen["kwargs"] = kwargs
            seen["check"] = check

        async def __aenter__(self):
            seen["entered"] = True
            return self

        async def __aexit__(self, *_args) -> bool:
            return False

        def connection(self):
            return _Connection()

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> bool:
            return False

        async def execute(self, sql: str) -> None:
            seen.setdefault("sql", []).append(sql)

    class _Saver:
        def __init__(self, *, conn) -> None:
            seen["saver_conn"] = conn

        async def setup(self) -> None:
            seen["setup_after_enter"] = seen.get("entered") is True

    monkeypatch.setattr(
        "agentkit.runtime.checkpointer.async_provider._postgres_components",
        lambda: (_Saver, _Pool, object()),
    )

    async def scenario() -> None:
        database = DatabaseConfig(
            backend="postgres",
            postgres_url="postgresql+asyncpg://localhost/app",
            postgres_schema="app",
        )
        async with make_checkpointer(AppConfig(database=database)) as saver:
            assert isinstance(saver, _Saver)
            assert seen["setup_after_enter"] is True

    asyncio.run(scenario())
    assert "+asyncpg" not in seen["conninfo"]
    assert seen["kwargs"]["autocommit"] is True
    assert seen["kwargs"]["prepare_threshold"] == 0
    assert seen["kwargs"]["keepalives"] == 1
    assert seen["kwargs"]["keepalives_idle"] == 60
    assert "search_path" in seen["conninfo"]
    assert any("CREATE SCHEMA" in sql for sql in seen["sql"])
