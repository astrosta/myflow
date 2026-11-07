"""The one-retry middleware: refresh the catalog, then retry once.

The refresh has to go through the runtime API that actually exists after Task 5
(`refresh_now`, with `request_refresh` as the fallback), be awaited *before* the
retry, and be bounded by `sync.tool_error_refresh_timeout_seconds`.  These tests
use runtime doubles with the real shape - an async refresh, no `refresh_and_sync`
- so a call to a deleted method fails here instead of silently swallowing an
AttributeError and skipping the retry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agentkit.agents.middlewares.tool_refresh_retry_middleware import ToolRefreshRetryMiddleware
from agentkit.contextforge.retry import RefreshableToolError, classify_tool_error


class FakeRuntime:
    """The runtime double, shaped like the real one: an awaited `refresh_now`.

    `refresh_and_sync` is deliberately absent - Task 5 deleted it - and
    `request_refresh` fails loudly if the middleware prefers it in async mode,
    which would let the retry race the refresh it depends on.
    """

    def __init__(self) -> None:
        self.synced: list[str] = []
        self.retries: list[tuple[str, str]] = []

    async def refresh_now(self, reason: str = "tool_error") -> None:
        self.synced.append(reason)

    async def request_refresh(self, reason: str) -> None:
        raise AssertionError("the middleware must await refresh_now, not request_refresh")

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self.retries.append((tool_name, result))


class LegacyRuntime:
    """A runtime that only exposes `request_refresh` - the documented fallback."""

    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.retries: list[tuple[str, str]] = []

    async def request_refresh(self, reason: str) -> None:
        self.refreshes.append(reason)

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self.retries.append((tool_name, result))


class ExplodingRuntime(FakeRuntime):
    """A refresh that fails: the caller keeps the original tool error."""

    async def refresh_now(self, reason: str = "tool_error") -> None:
        raise RuntimeError("contextforge unreachable")


class SlowRuntime(FakeRuntime):
    """A refresh that outlives the configured budget."""

    async def refresh_now(self, reason: str = "tool_error") -> None:
        await asyncio.sleep(5)


def _middleware(runtime: Any, *, timeout: float = 1) -> ToolRefreshRetryMiddleware:
    return ToolRefreshRetryMiddleware(
        enabled=True,
        refresh_timeout_seconds=timeout,
        runtime_provider=lambda: runtime,
    )


def _request(name: str = "github_search"):
    return SimpleNamespace(tool_call={"name": name, "id": "call-1", "args": {}})


def test_classifier_only_retries_not_found_and_invalid_args() -> None:
    assert classify_tool_error(RuntimeError("tool not found")) is RefreshableToolError.NOT_FOUND
    assert classify_tool_error(RuntimeError("invalid arguments")) is RefreshableToolError.INVALID_ARGUMENTS
    assert classify_tool_error(RuntimeError("request timeout")) is RefreshableToolError.NONE
    assert classify_tool_error(RuntimeError("unauthorized")) is RefreshableToolError.NONE
    assert classify_tool_error(ToolMessage(content="参数校验失败", tool_call_id="c")) is RefreshableToolError.INVALID_ARGUMENTS


@pytest.mark.asyncio
async def test_retryable_error_refreshes_once_and_retries() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tool not found")
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await _middleware(runtime).awrap_tool_call(_request(), handler)

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 2
    assert runtime.synced == ["tool_error"]
    assert runtime.retries == [("github_search", "success")]


@pytest.mark.asyncio
async def test_timeout_is_not_retried() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise RuntimeError("request timeout")

    with pytest.raises(RuntimeError):
        await _middleware(runtime).awrap_tool_call(_request(), handler)
    assert calls["n"] == 1
    assert runtime.synced == []


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_original_error() -> None:
    runtime = ExplodingRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise RuntimeError("tool not found")

    with pytest.raises(RuntimeError, match="tool not found"):
        await _middleware(runtime).awrap_tool_call(_request(), handler)
    assert calls["n"] == 1, "no retry without a completed refresh"
    assert runtime.retries == []


@pytest.mark.asyncio
async def test_a_slow_refresh_is_bounded_by_the_configured_timeout() -> None:
    runtime = SlowRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise RuntimeError("tool not found")

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(RuntimeError, match="tool not found"):
        await _middleware(runtime, timeout=0.05).awrap_tool_call(_request(), handler)
    elapsed = loop.time() - started

    assert calls["n"] == 1
    assert elapsed < 1.0, "the refresh budget, not the refresh, decides how long this takes"


@pytest.mark.asyncio
async def test_a_runtime_without_refresh_now_still_refreshes() -> None:
    runtime = LegacyRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tool not found")
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await _middleware(runtime).awrap_tool_call(_request(), handler)

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 2
    assert runtime.refreshes == ["tool_error"]


def test_sync_path_refreshes_once_and_retries() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tool not found")
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = _middleware(runtime).wrap_tool_call(_request(), handler)

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 2
    assert runtime.synced == ["tool_error"]
    assert runtime.retries == [("github_search", "success")]


def test_sync_path_timeout_is_not_retried() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise RuntimeError("request timeout")

    with pytest.raises(RuntimeError):
        _middleware(runtime).wrap_tool_call(_request(), handler)
    assert calls["n"] == 1
    assert runtime.synced == []
