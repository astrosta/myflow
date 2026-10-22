"""M3 §4.6 / M4 §9.2：补 thread 行的这一步不许把 run 拖下水，也不许静默漏建。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agentkit.runtime.runs.worker as worker
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.runtime.runs.manager import RunRecord
from agentkit.runtime.runs.schemas import RunStatus
from agentkit.runtime.user_context import MissingUserContextError, reset_current_user, set_current_user


def _run(scenario) -> None:
    async def _body() -> None:
        token = set_current_user(None)          # worker task 上没有 HTTP 的 ContextVar
        try:
            await scenario()
        finally:
            reset_current_user(token)

    asyncio.run(_body())


def _record(
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    *,
    user_id: str | None = "alice",
    metadata: dict | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id="lead_agent",
        status=RunStatus.pending,
        on_disconnect="continue",
        user_id=user_id,
        metadata={"topic": "demo", "myflow_trace_id": "trace-1"} if metadata is None else metadata,
    )


class _HangingStore:
    """get 永远不返回：用来验证超时和 abort 都不会卡住 run。"""

    async def get(self, *args, **kwargs):
        await asyncio.Event().wait()

    async def create(self, *args, **kwargs) -> None:
        raise AssertionError("create must not be reached")


class _BrokenStore:
    async def get(self, *args, **kwargs):
        raise RuntimeError("db down")

    async def create(self, *args, **kwargs) -> None:
        raise AssertionError("create must not be reached")


def test_missing_row_is_inserted_once_with_the_run_owner() -> None:
    async def scenario() -> None:
        store = MemoryThreadMetaStore()
        record = _record()

        await worker._ensure_thread_metadata(store, record, owner_user_id="alice", require_existing=False)
        row = await store.get("thread-1", user_id="alice")

        assert row is not None
        assert row["user_id"] == "alice"
        assert row["assistant_id"] == "lead_agent"
        assert row["status"] == "idle"
        assert row["metadata"] == {"topic": "demo"}, "run 级的 trace id 不该抄进 thread 行"

        # 第二次调用撞上已有的行：幂等返回，不插第二行
        await worker._ensure_thread_metadata(store, record, owner_user_id="alice", require_existing=False)
        again = await store.get("thread-1", user_id="alice")
        assert again is not None and again["incarnation"] == row["incarnation"]

    _run(scenario)


def test_strict_mode_reports_a_deleted_thread_and_does_not_recreate_it() -> None:
    async def scenario() -> None:
        store = MemoryThreadMetaStore()

        failure = await worker._ensure_thread_metadata_bounded(
            _record(thread_id="gone", run_id="run-gone"),
            store,
            owner_user_id="alice",
            require_existing=True,
        )

        assert isinstance(failure, LookupError)
        assert "gone" in str(failure)
        assert await store.get("gone", user_id="alice") is None

    _run(scenario)


def test_non_strict_mode_backfills_a_deleted_thread() -> None:
    async def scenario() -> None:
        store = MemoryThreadMetaStore()

        failure = await worker._ensure_thread_metadata_bounded(
            _record(thread_id="gone", run_id="run-gone"),
            store,
            owner_user_id="alice",
            require_existing=False,
        )

        assert failure is None
        assert await store.get("gone", user_id="alice") is not None

    _run(scenario)


def test_missing_owner_context_fails_instead_of_writing_a_global_row() -> None:
    async def scenario() -> None:
        store = MemoryThreadMetaStore()

        failure = await worker._ensure_thread_metadata_bounded(
            _record(thread_id="anon", run_id="run-anon", user_id=None),
            store,
            owner_user_id=None,
            require_existing=False,
        )

        assert isinstance(failure, MissingUserContextError)
        assert await store.get("anon", user_id=None) is None

    _run(scenario)


def test_hanging_store_times_out_without_blocking_the_run(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(worker, "THREAD_METADATA_SETUP_TIMEOUT_SECONDS", 0.05)

        failure = await worker._ensure_thread_metadata_bounded(
            _record(thread_id="slow", run_id="run-slow"),
            _HangingStore(),
            owner_user_id="alice",
            require_existing=False,
        )

        assert isinstance(failure, TimeoutError)

    _run(scenario)


def test_pending_abort_stops_waiting_for_metadata() -> None:
    async def scenario() -> None:
        record = _record(thread_id="abort", run_id="run-abort")
        record.abort_event.set()

        failure = await worker._ensure_thread_metadata_bounded(
            record,
            _HangingStore(),
            owner_user_id="alice",
            require_existing=False,
        )

        assert failure is None

    _run(scenario)


def test_strict_failure_fails_the_pending_start_and_still_enters_run_agent(monkeypatch) -> None:
    async def scenario() -> None:
        entered: list[str] = []

        async def fake_run_agent(bridge, run_manager, record, ctx) -> None:
            entered.append(record.run_id)

        monkeypatch.setattr(worker, "run_agent", fake_run_agent)
        run_manager = SimpleNamespace(fail_start_if_pending=AsyncMock())
        ctx = SimpleNamespace(thread_store=MemoryThreadMetaStore())
        record = _record(thread_id="gone", run_id="run-gone")

        await worker.run_after_metadata(
            record,
            ctx,
            bridge=SimpleNamespace(),
            run_manager=run_manager,
            owner_user_id="alice",
            require_existing_thread=True,
        )

        run_manager.fail_start_if_pending.assert_awaited_once()
        error = run_manager.fail_start_if_pending.await_args.kwargs["error"]
        assert "Thread verification failed" in error
        assert "LookupError" in error, "严格模式失败的原因是「行已删」，不是别的异常"
        assert entered == ["run-gone"], "终态和 END 只有 run_agent 这一条路径"

    _run(scenario)


def test_non_fatal_metadata_failure_only_warns(monkeypatch, caplog) -> None:
    async def scenario() -> None:
        entered: list[str] = []

        async def fake_run_agent(bridge, run_manager, record, ctx) -> None:
            entered.append(record.run_id)

        monkeypatch.setattr(worker, "run_agent", fake_run_agent)
        run_manager = SimpleNamespace(fail_start_if_pending=AsyncMock())
        ctx = SimpleNamespace(thread_store=_BrokenStore())

        with caplog.at_level(logging.WARNING):
            await worker.run_after_metadata(
                _record(thread_id="thread-1", run_id="run-1"),
                ctx,
                bridge=SimpleNamespace(),
                run_manager=run_manager,
                owner_user_id="alice",
                require_existing_thread=False,
            )

        assert "non-fatal" in caplog.text and "db down" in caplog.text
        run_manager.fail_start_if_pending.assert_not_awaited()
        assert entered == ["run-1"]

    _run(scenario)
