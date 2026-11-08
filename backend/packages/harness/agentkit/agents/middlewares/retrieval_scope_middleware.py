"""Run scope for the tool_search result set, and the v1 subagent boundary.

``ThreadState.retrieved_tool_names`` is **run** scoped: tool_search replaces it
with the cards it just returned, and tool_execute refuses any call name that is
not in it (design 8 / 9.1).  Two things have to hold for that gate to mean what
it says:

* a new run starts with an empty set.  Otherwise last run's cards stay callable
  forever, and "retrieval is the gate" quietly decays into "whatever was ever
  found is allowed" - ``RetrievalScopeMiddleware``.
* v1 subagents never get the two tools.  Retrieval and execution belong to the
  lead, whose answer the user reads (design 9.2).  A subagent declared with
  ``tools=None`` inherits the lead's table, so the boundary is enforced where
  that inheritance becomes a model schema - and again at call time, so a name the
  model emits from memory fails closed instead of running an unretrieved tool -
  ``SubagentRetrievalToolStripMiddleware``.

The names are literals on purpose: importing the two tool modules here would pull
the retrieval service and the MCP runtime into every assembly.
``tests/test_agent_factory.py`` pins them to the tools' own ``TOOL_NAME`` so the
pair can never drift.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.agents.thread_state import ThreadState

#: The two retrieval builtins.  Subagents (v1) never inherit them.
RETRIEVAL_TOOL_NAMES = frozenset({"tool_search", "tool_execute"})

SUBAGENT_UNAVAILABLE_MESSAGE = (
    "tool_search and tool_execute are not available inside a subagent. "
    "Report what you need - or what you could not do - to the lead instead."
)


class RetrievalScopeMiddleware(AgentMiddleware[ThreadState]):
    """Start every run with an empty retrieval set.

    The reducer replaces rather than merges, so writing ``[]`` is the reset.
    Every invocation counts as a new run here, including the continuation after
    an interrupt: a card retrieved before a pause has to be retrieved again,
    because a card the current turn has no budget for is exactly what the gate
    exists to refuse.  The model simply searches again.
    """

    def before_agent(self, state: ThreadState, runtime: Any) -> dict[str, Any]:
        del state, runtime
        return {"retrieved_tool_names": []}


class SubagentRetrievalToolStripMiddleware(AgentMiddleware[AgentState]):
    """Keep tool_search / tool_execute out of a subagent's hands (v1).

    ``tools=None`` subagents inherit the lead's tool table, which now contains
    both.  Hiding the schema is what stops the model from calling them; refusing
    the call is what stops a name produced from memory or from the brief.
    """

    def _strip(self, request: ModelRequest) -> ModelRequest:
        tools = list(getattr(request, "tools", None) or ())
        kept = [
            tool
            for tool in tools
            if getattr(tool, "name", None) not in RETRIEVAL_TOOL_NAMES
        ]
        if len(kept) == len(tools):
            return request
        return request.override(tools=kept)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._strip(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._strip(request))

    def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = getattr(request, "tool_call", None) or {}
        if not isinstance(tool_call, Mapping):
            return None
        name = str(tool_call.get("name") or "")
        if name not in RETRIEVAL_TOOL_NAMES:
            return None
        return ToolMessage(
            content=SUBAGENT_UNAVAILABLE_MESSAGE,
            tool_call_id=str(tool_call.get("id") or "missing_tool_call_id"),
            name=name,
            status="error",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Any:
        refused = self._refusal(request)
        return refused if refused is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        refused = self._refusal(request)
        return refused if refused is not None else await handler(request)


__all__ = [
    "RETRIEVAL_TOOL_NAMES",
    "RetrievalScopeMiddleware",
    "SUBAGENT_UNAVAILABLE_MESSAGE",
    "SubagentRetrievalToolStripMiddleware",
]
