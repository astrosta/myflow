"""The one-retry middleware: refresh the catalog, then retry once.

The refresh has to go through the runtime API that actually exists after Task 5
(`refresh_now`, with `request_refresh` as the fallback), be awaited *before* the
retry, and be bounded by `sync.tool_error_refresh_timeout_seconds`.  These tests
use runtime doubles with the real shape - an async refresh, no `refresh_and_sync`
- so a call to a deleted method fails here instead of silently swallowing an
AttributeError and skipping the retry.

The classifier is also the vocabulary tool_execute reports failures in, so part
of this file pins its one exception: an error that already carries
``origin=local_validation`` was handled by the executor's own fallback, and must
not be classified as refreshable even though its wording says "invalid
arguments" - a middleware retry would refresh a second time and re-run the call.

The marker alone is not enough while ``ToolRefreshRetryMiddleware`` is still
registered (it is deleted in Task 8, not before): tool_execute reports
``schema_mismatch`` and ``invalid_arguments`` in the same vocabulary, so the
middleware also skips the executor by name through
``retry.owns_retry_fallback``.  Those guards, and the signatures the executor
actually calls on the real ``ContextForgeRuntime``, are pinned below.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agentkit.agents.middlewares.tool_refresh_retry_middleware import ToolRefreshRetryMiddleware
from agentkit.config.contextforge_config import ContextForgeSyncConfig
from agentkit.contextforge.retry import (
    RefreshableToolError,
    classify_tool_error,
    owns_retry_fallback,
)
from agentkit.contextforge.runtime import ContextForgeRuntime
from agentkit.contextforge.sync import ContextForgeSyncStats


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


def _local_validation_message() -> ToolMessage:
    """The reply tool_execute produces for a locally invalid argument set."""
    payload = {
        "error": "arguments do not match the stored input_schema: invalid arguments",
        "origin": "local_validation",
        "retryable": True,
    }
    return ToolMessage(
        content=json.dumps(payload),
        tool_call_id="call-1",
        name="tool_execute",
        status="error",
    )


def _request(name: str = "github_search"):
    return SimpleNamespace(tool_call={"name": name, "id": "call-1", "args": {}})


def test_classifier_only_retries_not_found_and_invalid_args() -> None:
    assert classify_tool_error(RuntimeError("tool not found")) is RefreshableToolError.NOT_FOUND
    assert classify_tool_error(RuntimeError("invalid arguments")) is RefreshableToolError.INVALID_ARGUMENTS
    assert classify_tool_error(RuntimeError("request timeout")) is RefreshableToolError.NONE
    assert classify_tool_error(RuntimeError("unauthorized")) is RefreshableToolError.NONE
    assert classify_tool_error(ToolMessage(content="参数校验失败", tool_call_id="c")) is RefreshableToolError.INVALID_ARGUMENTS


def test_a_local_validation_error_is_not_refreshable() -> None:
    assert (
        classify_tool_error(_local_validation_message()) is RefreshableToolError.NONE
    ), "the executor already ran its fallback; a second refresh is not a fix"
    assert classify_tool_error("invalid arguments") is RefreshableToolError.INVALID_ARGUMENTS


@pytest.mark.asyncio
async def test_a_local_validation_result_is_not_retried() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return _local_validation_message()

    result = await _middleware(runtime).awrap_tool_call(_request("cf__github_search"), handler)

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 1, "no retry: tool_execute owns this fallback"
    assert runtime.synced == []
    assert runtime.retries == []


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

# --------------------------------------------------------------------------- #
# The executor owns its fallback: the middleware must not repeat it
# --------------------------------------------------------------------------- #


def _reply(origin: str, error: str, *, name: str = "tool_execute") -> ToolMessage:
    """A tool_execute-shaped failure reply: origin, retryable flag, error text."""
    payload = {
        "error": error,
        "origin": origin,
        "retryable": True,
    }
    return ToolMessage(
        content=json.dumps(payload),
        tool_call_id="call-1",
        name=name,
        status="error",
    )


def test_only_the_executor_is_a_self_healing_tool() -> None:
    assert owns_retry_fallback("tool_execute")
    assert owns_retry_fallback(" Tool_Execute "), "the check is normalized"
    assert not owns_retry_fallback("cf__github_search")
    assert not owns_retry_fallback("tool_search")
    assert not owns_retry_fallback(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "error"),
    [
        ("schema_mismatch", "invalid params: schema validation failed"),
        ("invalid_arguments", "invalid arguments for tool"),
        ("tool_not_found", "tool not found: github_search"),
        ("local_validation", "arguments do not match the stored input_schema"),
    ],
)
async def test_a_tool_execute_reply_is_never_refreshed_or_retried(
    origin: str, error: str
) -> None:
    """Every executor origin is final: it already ran the 9.1 fallback."""
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return _reply(origin, error)

    result = await _middleware(runtime).awrap_tool_call(
        _request("tool_execute"), handler
    )

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 1, "a second handler call is a duplicate upstream call"
    assert runtime.synced == [], "no second refresh"
    assert runtime.retries == []


@pytest.mark.asyncio
async def test_a_tool_execute_exception_is_never_refreshed_or_retried() -> None:
    """The name is known before the call, so a raised error is guarded too."""
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise RuntimeError("tool not found")

    with pytest.raises(RuntimeError, match="tool not found"):
        await _middleware(runtime).awrap_tool_call(_request("tool_execute"), handler)

    assert calls["n"] == 1
    assert runtime.synced == []


def test_the_sync_path_skips_tool_execute_too() -> None:
    runtime = FakeRuntime()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _reply("schema_mismatch", "invalid params: schema validation failed")

    result = _middleware(runtime).wrap_tool_call(_request("tool_execute"), handler)

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 1
    assert runtime.synced == []
    assert runtime.retries == []


@pytest.mark.asyncio
async def test_an_ordinary_tool_with_the_same_wording_is_still_retried() -> None:
    """The guard is the tool name: plain tools keep their one refresh and retry."""
    runtime = FakeRuntime()
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply(
                "invalid_arguments",
                "invalid arguments for tool",
                name="cf__github_search",
            )
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await _middleware(runtime).awrap_tool_call(
        _request("cf__github_search"), handler
    )

    assert isinstance(result, ToolMessage)
    assert calls["n"] == 2
    assert runtime.synced == ["tool_error"]
    assert runtime.retries == [("cf__github_search", "success")]


# --------------------------------------------------------------------------- #
# The real runtime, not a double: the executor's calls must keep existing
# --------------------------------------------------------------------------- #


def test_the_real_runtime_exposes_the_signatures_the_executor_calls() -> None:
    """tool_execute and this middleware call ContextForgeRuntime by name.

    A double cannot drift; the production class can.  These assertions read the
    real class, so renaming ``refresh_now``, losing the ``store``/``mcp_client``
    properties or making ``record_retry`` positional fails here instead of turning
    into a silent getattr fallback at runtime.
    """
    refresh = inspect.signature(ContextForgeRuntime.refresh_now).parameters
    assert asyncio.iscoroutinefunction(ContextForgeRuntime.refresh_now)
    assert list(refresh) == ["self", "reason"]
    assert (
        refresh["reason"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ), "the executor passes the reason positionally: refresh_now(REFRESH_REASON)"

    request = inspect.signature(ContextForgeRuntime.request_refresh).parameters
    assert asyncio.iscoroutinefunction(ContextForgeRuntime.request_refresh)
    assert list(request) == ["self", "reason"], (
        "request_refresh is the documented fallback, called positionally too"
    )
    assert request["reason"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    retry = inspect.signature(ContextForgeRuntime.record_retry).parameters
    assert list(retry) == ["self", "tool_name", "result"]
    assert retry["tool_name"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "the executor calls record_retry(tool_name=..., result=...)"
    )
    assert retry["result"].kind is inspect.Parameter.KEYWORD_ONLY

    for attribute in ("store", "mcp_client"):
        holder = inspect.getattr_static(ContextForgeRuntime, attribute, None)
        assert isinstance(holder, property), (
            f"tool_execute reads runtime.{attribute}; it must stay a property"
        )

    fields = {field.name for field in dataclasses.fields(ContextForgeSyncStats)}
    assert {"error", "index_failed"} <= fields, (
        "the executor decides whether a refresh replaced the snapshot from these"
    )

    timeout_field = "tool_error_refresh_timeout_seconds"
    assert timeout_field in ContextForgeSyncConfig.model_fields, (
        "the refresh budget the executor reads from config.sync must exist"
    )
