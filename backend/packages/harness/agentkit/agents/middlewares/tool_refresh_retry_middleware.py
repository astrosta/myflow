"""Refresh ContextForge tools once on not-found or invalid-argument errors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.contextforge.retry import (
    RefreshableToolError,
    classify_tool_error,
    owns_retry_fallback,
)


class ToolRefreshRetryMiddleware(AgentMiddleware[AgentState]):
    """One retry for the two error classes that can indicate a stale catalog.

    The middleware only serves *plain* tools.  tool_execute runs the design 9.1
    fallback itself: it refreshes, retries a missing tool exactly once, and
    answers every failure with its own origin.  Its reply text still matches the
    not-found and invalid-argument vocabulary, so classifying it like an ordinary
    tool error would refresh a second time and re-run tools/call for the same
    request - duplicate upstream execution, which 9.1 C.6 forbids.  The guard is
    by tool name, because the name is available even when the call raises.
    """

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
        """Refresh the catalog before the retry, bounded by the configured timeout.

        `refresh_now` is the runtime's awaited sync; it is required here because
        `request_refresh` honours `sync.admin_refresh_async` and would let the
        retry race the refresh it depends on.  `request_refresh` stays the
        fallback for a runtime that predates it.
        """
        runtime = self._runtime_provider()
        if runtime is None:
            return False
        try:
            refresh = getattr(runtime, "refresh_now", None)
            call = (
                refresh("tool_error")
                if refresh is not None
                else runtime.request_refresh("tool_error")
            )
            await asyncio.wait_for(call, timeout=self._timeout)
        except Exception:  # noqa: BLE001 - caller keeps the original error
            return False
        return True

    def _refresh_sync(self) -> bool:
        """Best-effort refresh for the synchronous tool path.

        LangGraph invokes synchronous tools off the async path; if a loop is
        already running here we cannot block it, so the original error is kept
        and the async wrapper remains the retry path.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self._refresh())
            except Exception:  # noqa: BLE001 - caller keeps the original error
                return False
            return True
        return False

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        if not self._enabled or owns_retry_fallback(self._name(request)):
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
        if not self._enabled or owns_retry_fallback(self._name(request)):
            return handler(request)
        try:
            result = handler(request)
        except (GraphBubbleUp, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - classify before deciding
            if classify_tool_error(exc) is RefreshableToolError.NONE:
                raise
            if not self._refresh_sync():
                raise
            retry_result = handler(request)
            self._record_retry(request, retry_result)
            return retry_result

        if classify_tool_error(result) is RefreshableToolError.NONE:
            return result
        if not self._refresh_sync():
            return result
        retry_result = handler(request)
        self._record_retry(request, retry_result)
        return retry_result

    def _record_retry(self, request: ToolCallRequest, result: Any) -> None:
        runtime = self._runtime_provider()
        if runtime is not None:
            runtime.record_retry(
                tool_name=self._name(request),
                result="success" if not _is_error(result) else "failed",
            )


def _is_error(value: Any) -> bool:
    if isinstance(value, ToolMessage):
        return value.status == "error"
    return isinstance(value, Exception)


__all__ = ["ToolRefreshRetryMiddleware"]
