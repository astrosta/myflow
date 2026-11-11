"""Validate knowledge citation markers on the lead's final answer.

The subagent returns a manifest of sources in its task ToolMessage. The lead may
copy, reorder, or paraphrase that answer; this middleware is the single place
that turns its markers into structured citations, and it drops any ID that was
not actually retrieved in this run.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from agentkit.agents.middlewares.delegation_ledger import RUN_START_MESSAGE_IDS_KEY
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
        known = collect_known_citations(_current_run_messages(messages, runtime))
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


def _current_run_messages(messages: list[Any], runtime: Any) -> list[Any]:
    """Drop messages that predate the current run.

    The run boundary is set by the worker in runtime.context. Without this
    filter, a reused thread would let an old task result keep its citations in
    the known set, so a later answer could persist an ID that this run never
    retrieved. An absent boundary preserves the old behavior for unit callers.
    """
    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        return messages
    raw_boundary = context.get(RUN_START_MESSAGE_IDS_KEY)
    if not isinstance(raw_boundary, Collection) or isinstance(raw_boundary, str | bytes):
        return messages
    boundary = {message_id for message_id in raw_boundary if isinstance(message_id, str)}
    if not boundary:
        return messages
    return [
        message
        for message in messages
        if getattr(message, "id", None) not in boundary
    ]


__all__ = ["KnowledgeCitationMiddleware"]
