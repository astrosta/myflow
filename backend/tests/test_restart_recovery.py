"""§9 restart: persisted history stays, inflight runs become error, memory ledger does not."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.recovery import RESTART_ERROR, recover_inflight_runs
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.runtime.events.catalog import LLM_HUMAN_INPUT
from agentkit.runtime.events.store.db import DbRunEventStore
from agentkit.runtime.events.store.memory import MemoryRunEventStore
from agentkit.runtime.runs.manager import RunManager
from agentkit.runtime.runs.schemas import ThreadOperationKind
from agentkit.runtime.runs.store import make_run_store


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


async def _seed(directory: Path) -> None:
    await init_engine("sqlite", url=_sqlite_url(directory), sqlite_dir=str(directory))
    factory = get_session_factory()
    runs = make_run_store(factory)
    threads = make_thread_meta_store(factory)
    events = DbRunEventStore(factory)
    await threads.create("thread-live", user_id="alice")
    await threads.update_status("thread-live", "running", user_id="alice")
    await threads.create("thread-done", user_id="alice")
    await runs.create_thread_operation_atomic(
        {
            "run_id": "run-live",
            "thread_id": "thread-live",
            "user_id": "alice",
            "status": "running",
            "metadata": {},
            "kwargs": {},
        },
        kind=ThreadOperationKind.run,
    )
    await runs.create_thread_operation_atomic(
        {
            "run_id": "run-done",
            "thread_id": "thread-done",
            "user_id": "alice",
            "status": "pending",
            "metadata": {},
            "kwargs": {},
        },
        kind=ThreadOperationKind.run,
    )
    await runs.update_run_completion("run-done", status="success")
    await events.put(
        {
            "thread_id": "thread-live",
            "run_id": "run-live",
            "user_id": "alice",
            "event_type": LLM_HUMAN_INPUT.event_type,
            "category": LLM_HUMAN_INPUT.category,
            "content": "记得这句话",
        }
    )
    await close_engine()


def test_restart_keeps_db_history_and_drops_a_memory_ledger(tmp_path: Path) -> None:
    async def scenario() -> None:
        await _seed(tmp_path)
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        runs = make_run_store(factory)
        threads = make_thread_meta_store(factory)
        events = DbRunEventStore(factory)
        manager = RunManager(runs)

        assert await recover_inflight_runs(runs, threads) == 1
        assert manager._runs == {}

        live = await runs.get("run-live", user_id="alice")
        done = await runs.get("run-done", user_id="alice")
        assert live is not None and live["status"] == "error" and live["error"] == RESTART_ERROR
        assert done is not None and done["status"] == "success"
        assert (await threads.get("thread-live", user_id="alice"))["status"] == "error"
        messages = await events.list_messages("thread-live", limit=10)
        assert [item["content"] for item in messages] == ["记得这句话"]

        looked_up = await manager.get("run-live", user_id="alice")
        assert looked_up is not None and looked_up.store_only and looked_up.task is None
        assert "run-live" not in manager._runs

        memory = MemoryRunEventStore()
        assert await memory.count_messages("thread-live") == 0

    _run(scenario)
