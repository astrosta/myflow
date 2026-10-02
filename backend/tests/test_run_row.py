"""§4.2 runs projection: columns, partial unique index, and the SQL write paths."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.models import RunRow
from agentkit.persistence.run.sql import SqlRunStore
from agentkit.runtime.runs.schemas import ThreadOperationKind
from agentkit.runtime.user_context import AUTO, MissingUserContextError


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


async def _store(tmp_path: Path) -> SqlRunStore:
    await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
    factory = get_session_factory()
    assert factory is not None
    return SqlRunStore(factory)


def _row(run_id: str, thread_id: str = "thread-1", **overrides) -> dict:
    row = {
        "run_id": run_id,
        "thread_id": thread_id,
        "user_id": "alice",
        "status": "pending",
        "metadata": {"topic": "中文"},
        "kwargs": {"config": {"model": "visible"}},
    }
    row.update(overrides)
    return row


def test_run_columns_match_the_projection() -> None:
    table = RunRow.__table__
    columns = {column.name: column for column in table.columns}
    assert set(columns) == {
        "run_id",
        "thread_id",
        "assistant_id",
        "user_id",
        "status",
        "operation_kind",
        "idempotency_key",
        "model_name",
        "multitask_strategy",
        "metadata_json",
        "kwargs_json",
        "error",
        "stop_reason",
        "message_count",
        "first_human_message",
        "last_ai_message",
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "llm_call_count",
        "lead_agent_tokens",
        "subagent_tokens",
        "middleware_tokens",
        "token_usage_by_model",
        "follow_up_to_run_id",
        "owner_worker_id",
        "lease_expires_at",
        "cancel_action",
        "cancel_requested_at",
        "created_at",
        "updated_at",
    }
    assert columns["run_id"].primary_key and columns["run_id"].type.length == 64
    assert columns["thread_id"].nullable is False and columns["thread_id"].index
    assert columns["user_id"].nullable and columns["user_id"].index
    assert columns["status"].default.arg == "pending"
    assert columns["operation_kind"].nullable is False
    assert columns["operation_kind"].default.arg == "run"
    assert "run" in str(columns["operation_kind"].server_default.arg)
    assert columns["idempotency_key"].nullable and columns["idempotency_key"].type.length == 255
    assert columns["model_name"].type.length == 128
    assert columns["multitask_strategy"].default.arg == "reject"
    assert columns["metadata_json"].default.arg.__wrapped__ is dict
    assert columns["kwargs_json"].default.arg.__wrapped__ is dict
    assert columns["token_usage_by_model"].default.arg.__wrapped__ is dict
    assert "{}" in str(columns["token_usage_by_model"].server_default.arg)
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True
    assert columns["updated_at"].onupdate is not None
    assert columns["lease_expires_at"].type.timezone is True

    indexes = {index.name: index for index in table.indexes}
    assert ("thread_id", "status") == tuple(indexes["ix_runs_thread_status"].columns.keys())
    assert ("lease_expires_at",) == tuple(indexes["ix_runs_lease"].columns.keys())
    assert indexes["uq_runs_idempotency_key"].unique
    active = indexes["uq_runs_thread_active"]
    assert active.unique
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        ddl = str(CreateIndex(active).compile(dialect=dialect))
        assert "UNIQUE" in ddl
        assert "WHERE" in ddl
        assert "pending" in ddl and "running" in ddl


def test_empty_database_allows_only_one_active_run_per_thread(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        async with factory() as session:
            ddl = await session.scalar(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_runs_thread_active'")
            )
        assert ddl is not None
        assert "UNIQUE" in ddl
        assert "WHERE" in ddl

        assert await store.create_thread_operation_atomic(
            _row("run-1"), kind=ThreadOperationKind.run
        )
        assert await store.create_thread_operation_atomic(
            _row("run-2"), kind=ThreadOperationKind.run
        ) is False
        assert await store.update_run_completion("run-1", status="success")
        assert await store.create_thread_operation_atomic(
            _row("run-3"), kind=ThreadOperationKind.run
        )

        async with factory() as session:
            session.add(RunRow(run_id="run-4", thread_id="thread-1", status="pending"))
            with pytest.raises(IntegrityError):
                await session.commit()

    _run(scenario)


def test_idempotency_key_conflict_returns_false(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        assert await store.create_thread_operation_atomic(
            _row("run-1", idempotency_key="same-key"), kind=ThreadOperationKind.run
        )
        assert await store.create_thread_operation_atomic(
            _row("run-2", thread_id="thread-2", idempotency_key="same-key"),
            kind=ThreadOperationKind.run,
        ) is False

    _run(scenario)


def test_supersede_active_interrupts_a_run_and_refuses_other_kinds(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        assert await store.create_thread_operation_atomic(
            _row("run-1"), kind=ThreadOperationKind.run
        )
        assert await store.create_thread_operation_atomic(
            _row("run-2"), kind=ThreadOperationKind.run, supersede_active=True
        )
        async with factory() as session:
            first = await session.get(RunRow, "run-1")
            second = await session.get(RunRow, "run-2")
            assert first is not None and first.status == "interrupted"
            assert second is not None and second.status == "pending"

        assert await store.create_thread_operation_atomic(
            _row("hold", thread_id="thread-2"), kind=ThreadOperationKind.checkpoint_write
        )
        assert await store.create_thread_operation_atomic(
            _row("run-3", thread_id="thread-2"),
            kind=ThreadOperationKind.run,
            supersede_active=True,
        ) is False
        async with factory() as session:
            assert await session.get(RunRow, "run-3") is None
            held = await session.get(RunRow, "hold")
            assert held is not None and held.status == "pending"

    _run(scenario)


def test_start_run_only_moves_pending_to_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        assert await store.start_run("run-1") is True
        assert await store.start_run("run-1") is False
        assert await store.start_run("missing") is False
        async with factory() as session:
            row = await session.get(RunRow, "run-1")
            assert row is not None and row.status == "running"

    _run(scenario)


def test_progress_writes_only_while_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        await store.update_run_progress("run-1", total_input_tokens=9)
        async with factory() as session:
            pending = await session.get(RunRow, "run-1")
            assert pending is not None
            assert pending.status == "pending"
            assert pending.total_input_tokens == 0

        assert await store.start_run("run-1")
        await store.update_run_progress(
            "run-1",
            total_input_tokens=4,
            total_output_tokens=6,
            llm_call_count=1,
            token_usage_by_model={"模型": {"input": 4, "output": 6}},
        )
        async with factory() as session:
            running = await session.get(RunRow, "run-1")
            assert running is not None
            assert running.status == "running"
            assert running.total_input_tokens == 4
            assert running.token_usage_by_model == {"模型": {"input": 4, "output": 6}}

        assert await store.update_run_completion("run-1", status="success", total_input_tokens=4)
        await store.update_run_progress("run-1", total_input_tokens=99)
        async with factory() as session:
            done = await session.get(RunRow, "run-1")
            assert done is not None
            assert done.status == "success"
            assert done.total_input_tokens == 4

    _run(scenario)


def test_completion_rejects_a_different_terminal_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        assert await store.update_run_completion("run-1", status="success", error=None, total_tokens=3)
        assert await store.update_run_completion("run-1", status="success", error="retry")
        assert await store.update_run_completion("run-1", status="error", error="late") is False
        async with factory() as session:
            row = await session.get(RunRow, "run-1")
            assert row is not None
            assert row.status == "success"
            assert row.error == "retry"
            assert row.total_tokens == 3

    _run(scenario)


def test_list_by_thread_pages_runs_with_a_keyset_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        await store.create_thread_operation_atomic(
            _row("run-a", created_at=base), kind=ThreadOperationKind.run
        )
        await store.update_run_completion("run-a", status="success")
        await store.create_thread_operation_atomic(
            _row("run-c", created_at=base), kind=ThreadOperationKind.run
        )
        await store.update_run_completion("run-c", status="success")
        await store.create_thread_operation_atomic(
            _row("run-b", created_at=base + timedelta(seconds=1)), kind=ThreadOperationKind.run
        )
        await store.update_run_completion("run-b", status="success")
        await store.create_thread_operation_atomic(
            _row("other", user_id="bob", created_at=base + timedelta(days=1)),
            kind=ThreadOperationKind.run,
        )
        await store.update_run_completion("other", status="success")
        await store.create_thread_operation_atomic(
            _row("hold", created_at=base + timedelta(seconds=2)),
            kind=ThreadOperationKind.checkpoint_write,
        )

        page = await store.list_by_thread("thread-1", user_id="alice", limit=2)
        assert [row["run_id"] for row in page] == ["run-b", "run-c"]
        assert page[0]["metadata"] == {"topic": "中文"}
        assert page[0]["kwargs"] == {"config": {"model": "visible"}}
        assert page[0]["created_at"].endswith("Z")

        cursor = f"{page[1]['created_at']}|{page[1]['run_id']}"
        older = await store.list_by_thread("thread-1", user_id="alice", limit=10, before=cursor)
        assert [row["run_id"] for row in older] == ["run-a"]

        everyone = await store.list_by_thread("thread-1", user_id=None, limit=10)
        assert {row["run_id"] for row in everyone} == {"run-a", "run-b", "run-c", "other"}
        with pytest.raises(MissingUserContextError, match=r"RunStore\.list_by_thread"):
            await store.list_by_thread("thread-1", user_id=AUTO)

    _run(scenario)


def test_model_name_is_stripped_and_truncated(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        long_name = "  " + ("模" * 200)
        await store.create_thread_operation_atomic(
            _row("run-1", model_name=long_name), kind=ThreadOperationKind.run
        )
        async with factory() as session:
            row = await session.get(RunRow, "run-1")
            assert row is not None
            assert row.model_name == "模" * 128
            assert len(row.model_name) == 128

    _run(scenario)


def test_message_previews_truncate_to_2000_characters(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        factory = get_session_factory()
        assert factory is not None
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        await store.start_run("run-1")
        preview = "字" * 2500
        await store.update_run_progress("run-1", first_human_message=preview, last_ai_message=preview)
        assert await store.update_run_completion(
            "run-1", status="success", first_human_message=preview, last_ai_message="ok"
        )
        async with factory() as session:
            row = await session.get(RunRow, "run-1")
            assert row is not None
            assert row.first_human_message == "字" * 2000
            assert row.last_ai_message == "ok"

    _run(scenario)
