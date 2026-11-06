"""Refresh ContextForge tools once on not-found or invalid-argument errors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.contextforge.retry import RefreshableToolError, classify_tool_error


class ToolRefreshRetryMiddleware(AgentMiddleware[AgentState]):
    """One retry for the two error classes that can indicate a stale catalog."""

    def __init__(
        self,
        *,
        enabled: bool,
        refresh_timeout_seconds: float,
        runtime_provider: Callable[[], Any | None],
    ) -> None:
        super().__init__()
        self._enabled = bool(enabled)
        self._timeout = float(refresh_timeout_seconds)
        self._runtime_provider = runtime_provider

    def _name(self, request: ToolCallRequest) -> str:
        tool_call = getattr(request, "tool_call", None) or {}
        name = tool_call.get("name") if isinstance(tool_call, dict) else None
        return str(name or "unknown_tool")

    async def _refresh(self) -> bool:
        runtime = self._runtime_provider()
        if runtime is None:
            return False
        try:
            await runtime.refresh_and_sync(
                reason="tool_error",
                force=True,
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 - caller keeps the original error
            return False
        return True

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        if not self._enabled:
            return await handler(request)
        retried = False
        try:
            result = await handler(request)
        except (GraphBubbleUp, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - classify before deciding
            if classify_tool_error(exc) is RefreshableToolError.NONE:
                raise
            if not await self._refresh():
                raise
            retried = True
            retry_result = await handler(request)
            runtime = self._runtime_provider()
            if runtime is not None:
                runtime.record_retry(
                    tool_name=self._name(request),
                    result="success" if not _is_error(retry_result) else "failed",
                )
            return retry_result

        if classify_tool_error(result) is RefreshableToolError.NONE:
            return result
        if retried or not await self._refresh():
            return result
        retry_result = await handler(request)
        runtime = self._runtime_provider()
        if runtime is not None:
            runtime.record_retry(
                tool_name=self._name(request),
                result="success" if not _is_error(retry_result) else "failed",
            )
        return retry_result

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Any:
        return handler(request)


def _is_error(value: Any) -> bool:
    if isinstance(value, ToolMessage):
        return value.status == "error"
    return isinstance(value, Exception)


__all__ = ["ToolRefreshRetryMiddleware"]
