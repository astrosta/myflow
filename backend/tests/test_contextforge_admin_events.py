"""ContextForge /admin/events parsing plus the runtime wiring around it.

The SSE endpoint is the only real-time external update signal: an event is a
signal to re-read ContextForge, never a tool definition, and only tool_*/gateway_*
events are worth a sync.  These tests cover the wire format and the runtime
subscription that turns an event into exactly one coalesced sync.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.admin_events import (
    parse_sse_block,
    should_refresh_tool_event,
)
from agentkit.contextforge.models import ContextForgeAdminEvent
from agentkit.contextforge.runtime import ContextForgeRuntime
from agentkit.contextforge.sync import ContextForgeSyncStats


def test_parse_multiline_event_and_keepalive() -> None:
    event = parse_sse_block(
        "event: tool_updated\ndata: {\"id\": \"t1\"}\n\ndata: ignored\n\n"
    )
    assert event is not None
    assert event.type == "tool_updated"
    assert event.data == {"id": "t1"}
    assert parse_sse_block(": keepalive\n\n") is None


def test_tool_and_gateway_events_refresh() -> None:
    assert should_refresh_tool_event("tool_updated") is True
    assert should_refresh_tool_event("gateway_activated") is True
    assert should_refresh_tool_event("tool_executed") is False


# --------------------------------------------------------------------------- #
# Runtime wiring: /admin/events is a signal, and the only real-time one
# --------------------------------------------------------------------------- #


class _Store:
    async def list_all(self, owner_user_id: str | None) -> list[Any]:
        return []


class _Service:
    """The sync service double: records every trigger the runtime asks for."""

    def __init__(self) -> None:
        self.store = _Store()
        self.calls: list[tuple[str, bool]] = []

    async def sync(self, reason: str = "manual", full: bool = False) -> ContextForgeSyncStats:
        self.calls.append((reason, full))
        return ContextForgeSyncStats()


class _Admin:
    async def aclose(self) -> None:
        return None


class _Subscriber:
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

    async def emit(self, event: ContextForgeAdminEvent) -> None:
        await self.callback(event)


def _config() -> ContextForgeConfig:
    return ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="adm",
        tool_loading="eager",
        search={"enabled": False},
        sync={"startup_sync": False},
        admin_events={"enabled": True, "debounce_ms": 0},
    )


def _runtime() -> tuple[ContextForgeRuntime, _Service, list[_Subscriber]]:
    """Build the runtime with a subscriber double that can replay an event."""
    service = _Service()
    subscribers: list[_Subscriber] = []

    def factory(config: ContextForgeConfig, callback: Any) -> _Subscriber:
        built = _Subscriber(config, callback)
        subscribers.append(built)
        return built

    runtime = ContextForgeRuntime(
        _config(),
        sync=service,  # type: ignore[arg-type]
        admin=_Admin(),  # type: ignore[arg-type]
        subscriber_factory=factory,
    )
    return runtime, service, subscribers


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was never reached")
        await asyncio.sleep(0.005)


async def _started_runtime() -> tuple[ContextForgeRuntime, _Service, _Subscriber]:
    runtime, service, subscribers = _runtime()
    await runtime.start()
    return runtime, service, subscribers[0]


@pytest.mark.asyncio
async def test_a_tool_event_triggers_a_sync() -> None:
    runtime, service, subscriber = await _started_runtime()
    try:
        await subscriber.emit(ContextForgeAdminEvent(type="tool_updated", data={"id": "t1"}))
        await _wait_until(lambda: service.calls == [("admin_events", False)])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_gateway_event_also_triggers_a_sync() -> None:
    runtime, service, subscriber = await _started_runtime()
    try:
        await subscriber.emit(ContextForgeAdminEvent(type="gateway_activated", data={}))
        await _wait_until(lambda: service.calls == [("admin_events", False)])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_tool_execution_event_does_not_trigger_a_sync() -> None:
    runtime, service, subscriber = await _started_runtime()
    try:
        await subscriber.emit(ContextForgeAdminEvent(type="tool_executed", data={"id": "t1"}))
        await asyncio.sleep(0.05)
        assert service.calls == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_the_runtime_subscribes_on_start_and_stops_the_reader() -> None:
    runtime, _service, subscriber = await _started_runtime()
    try:
        assert subscriber.started is True
        status = await runtime.status()
        assert status["admin_events"] == "connected"
    finally:
        await runtime.stop()
    assert subscriber.stopped is True
