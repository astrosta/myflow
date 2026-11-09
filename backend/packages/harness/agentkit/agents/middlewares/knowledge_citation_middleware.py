"""Validate knowledge citation markers on the lead's final answer.

The subagent returns a manifest of sources in its task ToolMessage. The lead may
copy, reorder, or paraphrase that answer; this middleware is the single place
that turns its markers into structured citations, and it drops any ID that was
not actually retrieved in this run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from agentkit.knowledge.citations import (
    CITATION_KEY,
    collect_known_citations,
    resolve_citations,
)
from agentkit.utils.messages import message_content_to_text


class KnowledgeCitationMiddleware(AgentMiddleware):
    """Attach additional_kwargs.knowledge_citations to the final AI message."""

    def after_model(self, state: Mapping[str, Any], runtime: Any) -> dict | None:
        messages = list(state.get("messages") or [])
        latest = _latest_ai_message(messages)
        if latest is None:
            return None
        # An intermediate tool-calling turn is not the user-facing answer. The
        # final answer arrives in a later model call, after the tool results are
        # in state and the markers can be resolved against them.
        if latest.tool_calls:
            return None
        text = message_content_to_text(latest.content)
        if not text.strip():
            return None
        known = collect_known_citations(messages)
        if not known:
            return None
        citations = resolve_citations(text, known)
        if not citations:
            return None
        kwargs = dict(latest.additional_kwargs or {})
        if kwargs.get(CITATION_KEY) == citations:
            return None
        kwargs[CITATION_KEY] = citations
        return {"messages": [latest.model_copy(update={"additional_kwargs": kwargs})]}

    async def aafter_model(self, state: Mapping[str, Any], runtime: Any) -> dict | None:
        return self.after_model(state, runtime)


def _latest_ai_message(messages: list[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


__all__ = ["KnowledgeCitationMiddleware"]
