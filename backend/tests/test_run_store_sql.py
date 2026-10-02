"""§4.5 SQL RunStore methods that were still open."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.run.sql import SqlRunStore
from agentkit.runtime.runs.schemas import RunStatus, ThreadOperationKind


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def _row(run_id: str, thread_id: str = "thread-1", **overrides) -> dict:
    row = {
        "run_id": run_id,
        "thread_id": thread_id,
        "user_id": "alice",
        "status": "pending",
        "metadata": {"topic": "中文"},
        "kwargs": {"config": {}},
    }
    row.update(overrides)
    return row


def test_update_status_only_changes_an_active_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = SqlRunStore(factory)
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        assert await store.update_status("missing", RunStatus.running) is False
        assert await store.update_status("run-1", RunStatus.running, stop_reason="go") is True
        loaded = await store.get("run-1", user_id="alice")
        assert loaded is not None
        assert loaded["status"] == "running"
        assert loaded["stop_reason"] == "go"
        assert loaded["metadata"] == {"topic": "中文"}
        assert await store.update_run_completion("run-1", status="success")
        assert await store.update_status("run-1", RunStatus.error, error="late") is False
        finished = await store.get("run-1", user_id=None)
        assert finished is not None and finished["status"] == "success"

    _run(scenario)


def test_get_filters_by_user_and_idempotency_key_does_not(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = SqlRunStore(factory)
        await store.create_thread_operation_atomic(
            _row("run-1", idempotency_key="scoped-key"), kind=ThreadOperationKind.run
        )
        assert await store.get("run-1", user_id="bob") is None
        assert (await store.get("run-1", user_id=None))["run_id"] == "run-1"
        assert (await store.get_by_idempotency_key("scoped-key"))["run_id"] == "run-1"
        assert await store.get_by_idempotency_key("missing") is None

    _run(scenario)


def test_list_inflight_ignores_user_and_terminal_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = SqlRunStore(factory)
        await store.create_thread_operation_atomic(_row("pending-alice"), kind=ThreadOperationKind.run)
        await store.update_run_completion("pending-alice", status="success")
        await store.create_thread_operation_atomic(
            _row("running-bob", thread_id="thread-2", user_id="bob"), kind=ThreadOperationKind.run
        )
        assert await store.start_run("running-bob")
        await store.create_thread_operation_atomic(
            _row("pending-cara", thread_id="thread-3", user_id="cara"), kind=ThreadOperationKind.run
        )
        found = {row["run_id"] for row in await store.list_inflight()}
        assert found == {"running-bob", "pending-cara"}

    _run(scenario)


def test_delete_thread_operation_refuses_run_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = SqlRunStore(factory)
        await store.create_thread_operation_atomic(_row("run-1"), kind=ThreadOperationKind.run)
        await store.update_run_completion("run-1", status="success")
        await store.create_thread_operation_atomic(
            _row("hold", thread_id="thread-2"), kind=ThreadOperationKind.checkpoint_write
        )
        with pytest.raises(ValueError, match="run"):
            await store.delete_thread_operation("run-1", user_id="alice")
        assert await store.get("run-1", user_id="alice") is not None
        await store.delete_thread_operation("hold", user_id="bob")
        assert await store.get("hold", user_id=None) is not None
        await store.delete_thread_operation("hold", user_id="alice")
        assert await store.get("hold", user_id=None) is None
        await store.delete_thread_operation("missing", user_id="alice")

    _run(scenario)
