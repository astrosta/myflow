"""Deny ContextForge tool calls that are not owned by the calling user.

Assembly already filters the model schema per user. This guard covers stale
graphs: a tool object loaded before another user claimed it must not remain
callable through an already-assembled agent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.contextforge.cache import get_contextforge_snapshot
from agentkit.runtime.user_context import resolve_runtime_user_id
from agentkit.tools.contextforge_metadata import is_contextforge_tool


class ContextForgeOwnershipMiddleware(AgentMiddleware[AgentState]):
    """Fail closed when a ContextForge tool is absent from the caller's catalog."""

    def _deny(self, request: ToolCallRequest) -> ToolMessage | None:
        tool = getattr(request, "tool", None)
        if tool is None or not is_contextforge_tool(tool):
            return None
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        user_id = resolve_runtime_user_id(context if isinstance(context, Mapping) else None)
        if user_id is None:
            return None
        snapshot = get_contextforge_snapshot()
        name = str(getattr(tool, "name", "") or "")
        entry = (
            next((item for item in snapshot.entries if item.name == name), None)
            if snapshot is not None
            else None
        )
        if entry is None or entry.owner != user_id:
            call = getattr(request, "tool_call", None) or {}
            call_id = call.get("id") if isinstance(call, Mapping) else None
            return ToolMessage(
                content="tool is not available to the current user",
                tool_call_id=str(call_id or "missing_tool_call_id"),
                name=name or "unknown_tool",
                status="error",
            )
        return None

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Any:
        denied = self._deny(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        denied = self._deny(request)
        return denied if denied is not None else await handler(request)


__all__ = ["ContextForgeOwnershipMiddleware"]