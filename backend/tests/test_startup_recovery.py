"""§9 startup: inflight rows become error, and the engine closes after the checkpointer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit.config.app_config import AppConfig
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.recovery import RESTART_ERROR, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime.events.store.memory import MemoryRunEventStore
from agentkit.runtime.runs.schemas import RunStatus, ThreadOperationKind
from agentkit.runtime.runs.store import make_run_store
from agentkit.runtime.runs.worker import _finalize
from backend.app.gateway import app as gateway_app


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def _row(run_id: str, thread_id: str, *, status: str) -> dict:
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "user_id": "alice",
        "status": status,
        "metadata": {},
        "kwargs": {},
    }


def test_startup_marks_inflight_runs_error_and_leaves_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        runs = make_run_store(factory)
        threads = make_thread_meta_store(factory)
        await threads.create("thread-live", user_id="alice")
        await threads.update_status("thread-live", "running", user_id="alice")
        await threads.create("thread-done", user_id="alice")
        assert await runs.create_thread_operation_atomic(
            _row("run-live", "thread-live", status="running"), kind=ThreadOperationKind.run
        )
        assert await runs.create_thread_operation_atomic(
            _row("run-done", "thread-done", status="pending"), kind=ThreadOperationKind.run
        )
        assert await runs.update_run_completion("run-done", status="success")

        assert await recover_inflight_runs(runs, threads) == 1

        live = await runs.get("run-live", user_id=None)
        done = await runs.get("run-done", user_id=None)
        assert live is not None and live["status"] == "error"
        assert live["error"] == RESTART_ERROR
        assert done is not None and done["status"] == "success"
        assert (await threads.get("thread-live", user_id="alice"))["status"] == "error"
        assert (await threads.get("thread-done", user_id="alice"))["status"] == "idle"
        assert await recover_inflight_runs(runs, threads) == 0

    _run(scenario)


def test_multiple_workers_refuse_to_mark_peer_runs() -> None:
    class Store:
        async def list_inflight(self):
            raise AssertionError("must not scan another worker's inflight rows")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="GATEWAY_WORKERS > 1"):
            await recover_inflight_runs(Store(), object(), gateway_workers=2)

    asyncio.run(scenario())


def test_completion_failure_is_not_swallowed() -> None:
    class Store:
        async def list_inflight(self):
            return [{"run_id": "run-1", "thread_id": "thread-1", "user_id": "alice"}]

        async def update_run_completion(self, run_id, *, status, error=None, **totals):
            raise RuntimeError("disk")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="disk"):
            await recover_inflight_runs(Store(), object())

    asyncio.run(scenario())


def test_rejected_completion_does_not_mark_the_thread() -> None:
    class Store:
        async def list_inflight(self):
            return [{"run_id": "run-1", "thread_id": "thread-1", "user_id": "alice"}]

        async def update_run_completion(self, run_id, *, status, error=None, **totals):
            return False

    class Threads:
        def __init__(self) -> None:
            self.calls = []

        async def update_status(self, thread_id, status, *, user_id=None) -> None:
            self.calls.append((thread_id, status, user_id))

    async def scenario() -> None:
        threads = Threads()
        assert await recover_inflight_runs(Store(), threads) == 0
        assert threads.calls == []

    asyncio.run(scenario())


def test_lifespan_closes_the_engine_after_the_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(gateway_app, "get_app_config", lambda: AppConfig())

    real_init = gateway_app.init_engine_from_config
    real_make = gateway_app.make_checkpointer
    real_close = gateway_app.close_engine

    async def init(db) -> None:
        order.append("init")
        await real_init(db)

    @asynccontextmanager
    async def make(config):
        order.append("checkpointer-enter")
        async with real_make(config) as saver:
            yield saver
        order.append("checkpointer-exit")

    async def recover(run_store, thread_meta, *, gateway_workers=1) -> int:
        order.append("recover")
        assert gateway_workers == 1
        return 0

    async def close() -> None:
        order.append("close")
        await real_close()

    monkeypatch.setattr(gateway_app, "init_engine_from_config", init)
    monkeypatch.setattr(gateway_app, "make_checkpointer", make)
    monkeypatch.setattr(gateway_app, "recover_inflight_runs", recover)
    monkeypatch.setattr(gateway_app, "close_engine", close)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with gateway_app.lifespan(app):
            order.append("yield")
            assert app.state.run_services.run_store is not None
            assert app.state.run_services.event_store is not None
            assert app.state.run_services.run_manager._runs == {}

    asyncio.run(scenario())
    assert order == ["init", "checkpointer-enter", "recover", "yield", "checkpointer-exit", "close"]


def test_recovery_error_fails_startup_and_still_closes_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []
    monkeypatch.setattr(gateway_app, "get_app_config", lambda: AppConfig())

    async def recover(run_store, thread_meta, *, gateway_workers=1) -> int:
        raise RuntimeError("orphan")

    real_close = gateway_app.close_engine

    async def close() -> None:
        closed.append("close")
        await real_close()

    monkeypatch.setattr(gateway_app, "recover_inflight_runs", recover)
    monkeypatch.setattr(gateway_app, "close_engine", close)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(RuntimeError, match="orphan"):
            async with gateway_app.lifespan(app):
                raise AssertionError("startup must not yield")

    asyncio.run(scenario())
    assert closed == ["close"]


def test_finalize_flushes_the_journal_before_completion() -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        record = SimpleNamespace(
            thread_id="thread-1",
            run_id="run-1",
            user_id="alice",
            abort_event=SimpleNamespace(is_set=lambda: False),
            abort_action=None,
            ownership_lost=True,
        )
        from agentkit.runtime.events.journal import RunJournal

        journal = RunJournal(store, record, batch_size=50)
        journal._buf.append(
            {
                "thread_id": "thread-1",
                "run_id": "run-1",
                "user_id": "alice",
                "event_type": "llm.human.input",
                "category": "message",
                "content": "hello",
                "metadata": {},
            }
        )
        await _finalize(
            None,
            None,
            record,
            None,
            terminal=RunStatus.error,
            error=None,
            error_type=None,
            rollback=None,
            usage=None,
            journal=journal,
        )
        assert await store.count_messages("thread-1") == 1
        assert journal._buf == []

    asyncio.run(scenario())
