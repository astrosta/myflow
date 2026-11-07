"""Hide ContextForge tools until search_tools promotes them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

from agentkit.tools.contextforge_metadata import is_contextforge_tool


class ToolSearchMiddleware(AgentMiddleware[AgentState]):
    """Selectively expose ContextForge tools from ThreadState.promoted.

    Registered inside the skill policy middleware so the policy still sees the
    complete tool table when it computes its baseline.
    """

    def __init__(self, *, deferred: bool = True) -> None:
        super().__init__()
        self._deferred = bool(deferred)

    def _visible(self, request: ModelRequest) -> ModelRequest:
        if not self._deferred:
            return request
        state = request.state if isinstance(request.state, Mapping) else {}
        promoted = set(state.get("promoted") or [])
        tools = [
            tool
            for tool in (getattr(request, "tools", None) or [])
            if not is_contextforge_tool(tool)
            or getattr(tool, "name", None) in promoted
        ]
        return request.override(tools=tools)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._visible(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._visible(request))


__all__ = ["ToolSearchMiddleware"]
