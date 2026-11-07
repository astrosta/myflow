"""ContextForge runtime lifecycle: startup, refresh coalescing, midnight, status.

The runtime owns *when* a sync happens (startup, management write, /admin/events,
local midnight, manual reindex); the Task 4 ContextForgeSyncService owns the
*serialization* - one process lock - so the runtime must never grow a second
lock.  These tests pin that split: every trigger funnels into one service call,
events debounce and coalesce, an event that arrives during a running sync marks
the runtime dirty and causes exactly one follow-up pass, the midnight task is a
real task that stop() cancels, and status comes from the authoritative store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import agentkit.contextforge.daily as daily
from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.cache import (
    get_contextforge_snapshot,
    invalidate_contextforge_cache,
)
from agentkit.contextforge.runtime import (
    ContextForgeRuntime,
    build_contextforge_runtime,
    get_contextforge_runtime,
    reset_contextforge_runtime,
)
from agentkit.contextforge.sync import ContextForgeSyncStats
from agentkit.persistence.schema import Base

LOCAL = ZoneInfo("Asia/Shanghai")


def _config(**overrides: Any) -> ContextForgeConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "http://cf",
        "admin_token": "adm",
        "tool_loading": "eager",
        "search": {"enabled": False},
        "sync": {"startup_sync": True},
        "admin_events": {"enabled": True, "debounce_ms": 0},
    }
    base.update(overrides)
    return ContextForgeConfig(**base)


def _row(*, callable_: bool = True) -> SimpleNamespace:
    return SimpleNamespace(name="demo", callable=callable_)


class FakeStore:
    """The authoritative catalog reader status() uses; never an in-process snapshot."""

    def __init__(self, rows: Any = (), *, error: Exception | None = None) -> None:
        self.rows = list(rows)
        self.error = error
        self.reads = 0

    async def list_all(self, owner_user_id: str | None) -> list[Any]:
        assert owner_user_id is None, "status is the admin read: it lists every owner"
        self.reads += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class FakeSyncService:
    """Stands in for the Task 4 service: records triggers, never raises."""

    def __init__(self, rows: Any = (), *, error: str | None = None) -> None:
        self.store = FakeStore(rows)
        self.error = error
        self.calls: list[tuple[str, bool]] = []
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def sync(self, reason: str = "manual", full: bool = False) -> ContextForgeSyncStats:
        self.calls.append((reason, full))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return ContextForgeSyncStats(scanned=len(self.store.rows), error=self.error)


class FakeAdmin:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeSubscriber:
    def __init__(self, config: ContextForgeConfig, callback: Any) -> None:
        self.config = config
        self.callback = callback
        self.started = False
        self.stopped = False
        self.status = "disabled"

    async def start(self) -> None:
        self.started = True
        self.status = "connected"

    async def stop(self) -> None:
        self.stopped = True
        self.status = "disabled"


class SubscriberFactory:
    def __init__(self) -> None:
        self.created: list[FakeSubscriber] = []

    def __call__(self, config: ContextForgeConfig, callback: Any) -> FakeSubscriber:
        subscriber = FakeSubscriber(config, callback)
        self.created.append(subscriber)
        return subscriber


def _runtime(
    config: ContextForgeConfig | None = None,
    *,
    rows: Any = (),
    error: str | None = None,
    daily_delay: Any = None,
) -> tuple[ContextForgeRuntime, FakeSyncService, FakeAdmin, SubscriberFactory]:
    service = FakeSyncService(rows, error=error)
    admin = FakeAdmin()
    factory = SubscriberFactory()
    runtime = ContextForgeRuntime(
        config or _config(),
        sync=service,
        admin=admin,
        subscriber_factory=factory,
        daily_delay=daily_delay,
    )
    return runtime, service, admin, factory


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never reached")
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_syncs_once_then_subscribes_and_schedules_midnight() -> None:
    runtime, service, _admin, factory = _runtime(rows=[_row()])
    await runtime.start()
    try:
        assert service.calls == [("startup", False)]
        assert [subscriber.started for subscriber in factory.created] == [True]
        status = await runtime.status()
        assert status["sync"] == "ready"
        assert status["admin_events"] == "connected"
        assert status["daily"] == "running"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    runtime, service, _admin, factory = _runtime()
    await runtime.start()
    try:
        await runtime.start()
        assert service.calls == [("startup", False)]
        assert len(factory.created) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_startup_sync_can_be_disabled() -> None:
    runtime, service, _admin, _factory = _runtime(_config(sync={"startup_sync": False}))
    await runtime.start()
    try:
        assert service.calls == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_disabled_runtime_never_touches_contextforge() -> None:
    runtime, service, admin, factory = _runtime(_config(enabled=False))

    await runtime.start()
    await runtime.request_refresh("admin")
    runtime.request_refresh_async("admin")
    status = await runtime.status()

    assert status["enabled"] is False
    assert status["daily"] == "disabled"
    assert status["admin_events"] == "disabled"
    assert service.calls == []
    assert factory.created == []
    assert admin.closed is False
    await runtime.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_midnight_task_and_the_subscriber() -> None:
    delays = iter([3600.0 for _ in range(10)])
    runtime, service, admin, factory = _runtime(daily_delay=lambda: next(delays))
    await runtime.start()
    await runtime.stop()

    assert factory.created[0].stopped is True
    assert admin.closed is True
    status = await runtime.status()
    assert status["daily"] == "stopped"
    assert status["admin_events"] == "disabled"

    # Stopping twice must stay harmless: app shutdown paths can overlap.
    await runtime.stop()
    assert service.calls == [("startup", False)]


# --------------------------------------------------------------------------- #
# Refresh triggers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_management_write_can_force_a_synchronous_refresh() -> None:
    runtime, service, _admin, _factory = _runtime(
        _config(sync={"startup_sync": False, "admin_refresh_async": False})
    )
    await runtime.request_refresh("admin")
    assert service.calls == [("admin", False)]


@pytest.mark.asyncio
async def test_a_management_write_can_request_an_async_refresh() -> None:
    runtime, service, _admin, _factory = _runtime(_config(sync={"startup_sync": False}))
    await runtime.request_refresh("admin")
    await _wait_until(lambda: service.calls == [("admin", False)])


@pytest.mark.asyncio
async def test_a_burst_of_triggers_is_debounced_into_one_sync() -> None:
    runtime, service, _admin, _factory = _runtime(
        _config(sync={"startup_sync": False}, admin_events={"debounce_ms": 100})
    )

    for _ in range(3):
        runtime.request_refresh_async("admin_events")

    await asyncio.sleep(0.03)
    assert service.calls == [], "the debounce window must hold the sync back"

    await _wait_until(lambda: len(service.calls) >= 1)
    await asyncio.sleep(0.05)
    assert service.calls == [("admin_events", False)]


@pytest.mark.asyncio
async def test_triggers_during_a_sync_mark_dirty_and_run_exactly_one_more_pass() -> None:
    runtime, service, _admin, _factory = _runtime(
        _config(sync={"startup_sync": False}, admin_events={"debounce_ms": 0})
    )
    service.gate = asyncio.Event()

    runtime.request_refresh_async("admin_events")
    await asyncio.wait_for(service.started.wait(), timeout=1)

    # Two more signals arrive while the first sync is still running: they must
    # collapse into a single follow-up pass, not a queue of two.
    runtime.request_refresh_async("admin_events")
    runtime.request_refresh_async("admin_events")

    service.gate.set()
    await _wait_until(lambda: len(service.calls) >= 2, timeout=5)
    await asyncio.sleep(0.1)
    assert service.calls == [("admin_events", False), ("admin_events", False)]


@pytest.mark.asyncio
async def test_the_midnight_task_runs_a_full_sync() -> None:
    delays = iter([0.02, 3600.0, 3600.0])
    runtime, service, _admin, _factory = _runtime(daily_delay=lambda: next(delays))

    await runtime.start()
    try:
        await _wait_until(
            lambda: any(reason == "midnight" for reason, _ in service.calls), timeout=2
        )
        assert ("midnight", True) in service.calls
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_reindex_runs_a_full_sync() -> None:
    runtime, service, _admin, _factory = _runtime()

    stats = await runtime.reindex()

    assert service.calls == [("manual", True)]
    assert stats.error is None


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_counts_come_from_the_store_not_an_in_memory_snapshot() -> None:
    invalidate_contextforge_cache("test")
    runtime, _service, _admin, _factory = _runtime(
        rows=[_row(), _row(callable_=False), _row()]
    )

    status = await runtime.status()

    assert status["indexed_tools"] == 2
    assert status["total_tools"] == 3
    assert get_contextforge_snapshot() is None, "the old in-memory snapshot is gone"


@pytest.mark.asyncio
async def test_status_degrades_when_the_catalog_cannot_be_read() -> None:
    runtime, service, _admin, _factory = _runtime()
    service.store.error = RuntimeError("db down")

    status = await runtime.status()

    assert status["index"] == "degraded"
    assert status["indexed_tools"] == 0
    assert status["total_tools"] == 0


def test_building_a_disabled_runtime_returns_none() -> None:
    assert build_contextforge_runtime(ContextForgeConfig()) is None


@pytest.mark.asyncio
async def test_building_an_enabled_runtime_wires_the_authoritative_store(
    tmp_path: Any,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "contextforge.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = build_contextforge_runtime(_config(), session_factory=factory)
    assert runtime is not None
    try:
        assert get_contextforge_runtime() is runtime
        status = await runtime.status()
        assert status["total_tools"] == 0
        assert status["indexed_tools"] == 0
    finally:
        reset_contextforge_runtime()
        await runtime.admin.aclose()
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Local midnight arithmetic
# --------------------------------------------------------------------------- #


def test_next_local_midnight_is_the_next_local_day_in_the_given_zone() -> None:
    now = datetime(2026, 11, 7, 23, 30, tzinfo=UTC)

    target = daily.next_local_midnight(now, "UTC")

    assert target == datetime(2026, 11, 8, 0, 0, tzinfo=UTC)


def test_next_local_midnight_follows_the_configured_zone() -> None:
    now = datetime(2026, 11, 7, 15, 30, tzinfo=UTC)  # 23:30 in Shanghai

    target = daily.next_local_midnight(now, LOCAL)

    assert target == datetime(2026, 11, 8, 0, 0, tzinfo=LOCAL)
    assert target.astimezone(UTC) == datetime(2026, 11, 7, 16, 0, tzinfo=UTC)


def test_next_local_midnight_after_exactly_midnight_rolls_a_day() -> None:
    now = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)

    assert daily.next_local_midnight(now, "UTC") == datetime(2026, 11, 9, 0, 0, tzinfo=UTC)


def test_next_local_midnight_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        daily.next_local_midnight(datetime(2026, 11, 8, 0, 0), "UTC")


def test_daily_delay_honours_daily_at() -> None:
    now = datetime(2026, 11, 7, 7, 0, tzinfo=UTC)

    assert daily.next_daily_run(now, "UTC", "06:30") == datetime(
        2026, 11, 8, 6, 30, tzinfo=UTC
    )


def test_daily_delay_adds_bounded_jitter() -> None:
    now = datetime(2026, 11, 7, 23, 0, tzinfo=UTC)

    assert daily.daily_delay(now, timezone="UTC", jitter_seconds=0) == 3600.0
    assert daily.daily_delay(
        now, timezone="UTC", jitter_seconds=60, rand=lambda: 0.5
    ) == 3630.0
