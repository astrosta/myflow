"""Structured retrieval events: one normalized payload per retrieval tool result.

Evaluation needs the final model-visible retrieval result as data, not as text
scraped back out of ``llm.tool.result`` or an 8192-character-truncated
``subagent.step``.  This module owns the definition-to-tool mapping and the
single normalizer that turns a tool result into that payload.  It is pure: no
store, no journal, no LangGraph.  Emitters (Task 3/4) call
:func:`retrieval_event_for_tool` and write the returned event themselves.

The normalizer is deliberately naive about *why* a payload looks wrong.  A tool
result is untrusted input: malformed JSON, an error payload, a partially
streamed message, an unknown tool name - all of them return ``None`` instead of
raising, because a retrieval event is evidence, never a reason to fail a turn.
Per-entry validation follows the same rule: a card or chunk without its
identity key (call_name / skill_id / chunk_id+document_id) is dropped rather
than salvaging a half-row.

Only JSON-safe, final model-visible fields cross this boundary.  Tool schemas,
descriptions of tools, headers, and credentials never appear in the payload -
a retrieval event is read by the evaluation pipeline and the run-events API, so
it must stay small and free of anything the model did not see.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentkit.runtime.events.catalog import (
    RETRIEVAL_KB_EVENT,
    RETRIEVAL_SKILL_EVENT,
    RETRIEVAL_TOOL_EVENT,
    RunEventDefinition,
)
from agentkit.utils.messages import message_content_to_text

TOOL_SEARCH_TOOL_NAME = "tool_search"
SEARCH_SKILLS_TOOL_NAME = "search_skills"
KNOWLEDGE_SEARCH_TOOL_NAME = "knowledge_search"

#: The one place that says which tool result becomes which event type.
RETRIEVAL_TOOL_EVENTS: dict[str, RunEventDefinition] = {
    TOOL_SEARCH_TOOL_NAME: RETRIEVAL_TOOL_EVENT,
    SEARCH_SKILLS_TOOL_NAME: RETRIEVAL_SKILL_EVENT,
    KNOWLEDGE_SEARCH_TOOL_NAME: RETRIEVAL_KB_EVENT,
}


@dataclass(frozen=True, slots=True)
class RetrievalEvent:
    """A normalized retrieval payload ready to be written as a run event."""

    event_type: str
    content: dict[str, Any]


def retrieval_event_for_tool(*, tool_name: str, content: Any) -> RetrievalEvent | None:
    """Normalize one retrieval tool result, or ``None`` when it cannot be one.

    ``content`` is what a ToolMessage carries: a JSON string, a list of
    ``{"type": "text", "text": ...}`` blocks, or an already-parsed mapping.
    """
    definition = RETRIEVAL_TOOL_EVENTS.get(tool_name) if isinstance(tool_name, str) else None
    if definition is None:
        return None
    try:
        payload = _payload(content)
        if payload is None:
            return None
        normalized = _NORMALIZERS[definition.event_type](payload)
    except Exception:  # noqa: BLE001 - a tool result must never fail a turn
        return None
    if normalized is None:
        return None
    return RetrievalEvent(event_type=definition.event_type, content=normalized)


def _payload(content: Any) -> Mapping[str, Any] | None:
    """Read a tool result as a JSON object, using the message text extractor."""

    if isinstance(content, Mapping):
        return content
    text = message_content_to_text(content)
    if not text.strip():
        return None
    parsed = json.loads(text)
    return parsed if isinstance(parsed, Mapping) else None


def _tool_content(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """tool_search: cards that survived the output budget, plus the channels."""

    raw = payload.get("tools")
    if not isinstance(raw, list):
        return None
    retrieval = _mapping(payload.get("retrieval"))
    return {
        "tools": [card for card in map(_tool_card, raw) if card is not None],
        "channels": _strings(retrieval.get("channels")),
        "warnings": _strings(retrieval.get("warnings")),
        "truncated": _truncated(payload, retrieval),
    }


def _tool_card(item: object) -> dict[str, Any] | None:
    """One tool card, reduced to what evaluation and tool_execute care about."""

    if not isinstance(item, Mapping):
        return None
    call_name = _text(item.get("call_name"))
    if not call_name:
        return None
    return {
        "tool_id": _text(item.get("tool_id")),
        "call_name": call_name,
        "score": _score(item.get("score")),
    }


def _skill_content(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """search_skills: the metadata list; the skill body belongs to load_skill."""

    raw = payload.get("skills")
    if not isinstance(raw, list):
        return None
    retrieval = _mapping(payload.get("retrieval"))
    return {
        "skills": [skill for skill in map(_skill, raw) if skill is not None],
        "channels": _strings(retrieval.get("channels")),
        "warnings": _strings(retrieval.get("warnings")),
        "truncated": _truncated(payload, retrieval),
    }


def _skill(item: object) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    skill_id = _text(item.get("skill_id"))
    if not skill_id:
        return None
    return {
        "skill_id": skill_id,
        "name": _text(item.get("name")),
        "score": _score(item.get("score")),
    }


def _kb_content(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """knowledge_search: the final reranked, possibly truncated chunks."""

    raw = payload.get("results")
    if not isinstance(raw, list):
        return None
    return {
        "query": _text(payload.get("query")),
        "keyword_query": _optional_text(payload.get("keyword_query")),
        "retrieval_mode": _text(payload.get("retrieval_mode")),
        "reranked": payload.get("reranked") is True,
        "items": [item for item in map(_kb_item, raw) if item is not None],
        "truncated": _truncated(payload, None),
    }


def _kb_item(item: object) -> dict[str, Any] | None:
    """One chunk. content 原样保留——评测的 Faithfulness 依赖未改写的正文。"""

    if not isinstance(item, Mapping):
        return None
    document_id = _text(item.get("document_id"))
    chunk_id = _text(item.get("chunk_id"))
    if not document_id or not chunk_id:
        return None
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "citation_id": _text(item.get("citation_id")),
        "filename": _text(item.get("filename")),
        "section": _text(item.get("section")),
        "page": _non_negative_int(item.get("page")),
        "score": _score(item.get("score")),
        "rerank_score": _optional_score(item.get("rerank_score")),
        "content": _raw_text(item.get("content")),
    }


_NORMALIZERS = {
    RETRIEVAL_TOOL_EVENT.event_type: _tool_content,
    RETRIEVAL_SKILL_EVENT.event_type: _skill_content,
    RETRIEVAL_KB_EVENT.event_type: _kb_content,
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _raw_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _score(value: object) -> float:
    number = _finite(value)
    return 0.0 if number is None else number


def _optional_score(value: object) -> float | None:
    return _finite(value)


def _finite(value: object) -> float | None:
    """JSON has no NaN/Infinity; a non-number is not a score."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _truncated(payload: Mapping[str, Any], retrieval: Mapping[str, Any] | None) -> bool:
    """A truncated retrieval must stay flagged: evaluation refuses to score it."""

    if payload.get("truncated") is True:
        return True
    return retrieval is not None and retrieval.get("truncated") is True


__all__ = [
    "KNOWLEDGE_SEARCH_TOOL_NAME",
    "RETRIEVAL_TOOL_EVENTS",
    "RetrievalEvent",
    "SEARCH_SKILLS_TOOL_NAME",
    "TOOL_SEARCH_TOOL_NAME",
    "retrieval_event_for_tool",
]
