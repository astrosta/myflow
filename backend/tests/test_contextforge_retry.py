from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from agentkit.agents.middlewares.tool_refresh_retry_middleware import ToolRefreshRetryMiddleware
from agentkit.contextforge.retry import RefreshableToolError, classify_tool_error


class FakeRuntime:
    def __init__(self) -> None:
        self.refreshes = 0
        self.retries: list[tuple[str, str]] = []

    async def refresh_and_sync(self, **kwargs):
        self.refreshes += 1

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self.retries.append((tool_name, result))


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
    middleware = ToolRefreshRetryMiddleware(
        enabled=True,
        refresh_timeout_seconds=1,
        runtime_provider=lambda: runtime,
    )
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tool not found")
        return ToolMessage(content="ok", tool_call_id="call-1")

    result = await middleware.awrap_tool_call(_request(), handler)
    assert isinstance(result, ToolMessage)
    assert calls["n"] == 2
    assert runtime.refreshes == 1


@pytest.mark.asyncio
async def test_timeout_is_not_retried() -> None:
    runtime = FakeRuntime()
    middleware = ToolRefreshRetryMiddleware(
        enabled=True,
        refresh_timeout_seconds=1,
        runtime_provider=lambda: runtime,
    )
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        raise RuntimeError("request timeout")

    with pytest.raises(RuntimeError):
        await middleware.awrap_tool_call(_request(), handler)
    assert calls["n"] == 1
    assert runtime.refreshes == 0
