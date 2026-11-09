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
A reply that says retrieval did not run (``tool_search``'s unavailable index or
``catalog_unavailable`` warning, ``search_skills``'s message-only unavailable or
blank-query reply) also returns ``None``: "could not retrieve" must never be
recorded as "retrieved nothing".  A successful tool/skill answer therefore has
to carry a ``retrieval`` mapping; a skill answer must not look like a service
failure (see :func:`_skill_search_unavailable`); and a KB answer has to carry
its query plus chunks with non-empty content.  Per-entry validation drops a
card or chunk without its identity key or content rather than salvaging a
half-row, and a non-empty results list that yields no usable row is malformed,
not an empty success.

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

#: ``tool_search`` reports "retrieval did not run" with this index kind or this
#: warning; both are failure reports, not empty answers.  Mirrors
#: ``agentkit.tools.search.service`` without importing the tool package here.
UNAVAILABLE_RETRIEVAL_INDEX = "unavailable"
CATALOG_UNAVAILABLE_WARNING = "catalog_unavailable"

#: ``search_skills`` reports a service failure with one of these warnings and no
#: recall channel.  Mirrors ``agentkit.skills.search`` without importing the
#: skill runtime here.
RETRIEVAL_UNAVAILABLE_WARNING = "retrieval_unavailable"
VECTOR_UNAVAILABLE_WARNING = "vector_unavailable"
KEYWORD_UNAVAILABLE_WARNING = "keyword_unavailable"
SKILL_SEARCH_FAILURE_WARNINGS = frozenset(
    {
        RETRIEVAL_UNAVAILABLE_WARNING,
        VECTOR_UNAVAILABLE_WARNING,
        KEYWORD_UNAVAILABLE_WARNING,
    }
)

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
    retrieval = _retrieval_marker(payload)
    if retrieval is None or _retrieval_unavailable(retrieval):
        return None
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
    # Disabled, unavailable and blank-query replies are message-only: no
    # retrieval mapping, so they are not a successful empty search.
    retrieval = _retrieval_marker(payload)
    if retrieval is None or _skill_search_unavailable(retrieval):
        return None
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
    skill: dict[str, Any] = {"skill_id": skill_id, "name": _text(item.get("name"))}
    score = _optional_score(item.get("score"))
    if score is not None:
        # search_skills does not rank its hits today; never invent a score.
        skill["score"] = score
    return skill


def _kb_content(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """knowledge_search: the final reranked, possibly truncated chunks."""

    raw = payload.get("results")
    if not isinstance(raw, list):
        return None
    query = _text(payload.get("query"))
    if not query:
        return None
    items = [item for item in map(_kb_item, raw) if item is not None]
    if raw and not items:
        # The tool claimed results but none of them is usable: that is a
        # malformed payload, not a successful empty retrieval.
        return None
    return {
        "query": query,
        "keyword_query": _optional_text(payload.get("keyword_query")),
        "retrieval_mode": _text(payload.get("retrieval_mode")),
        "reranked": payload.get("reranked") is True,
        "items": items,
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
    content = _raw_text(item.get("content"))
    if not content.strip():
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
        "content": content,
    }


_NORMALIZERS = {
    RETRIEVAL_TOOL_EVENT.event_type: _tool_content,
    RETRIEVAL_SKILL_EVENT.event_type: _skill_content,
    RETRIEVAL_KB_EVENT.event_type: _kb_content,
}


def _retrieval_marker(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """A successful skill/tool answer always says how the search ran."""

    retrieval = payload.get("retrieval")
    return retrieval if isinstance(retrieval, Mapping) else None


def _retrieval_unavailable(retrieval: Mapping[str, Any]) -> bool:
    """tool_search's two machine-readable "did not retrieve" markers."""

    if retrieval.get("index") == UNAVAILABLE_RETRIEVAL_INDEX:
        return True
    return CATALOG_UNAVAILABLE_WARNING in _strings(retrieval.get("warnings"))


def _skill_search_unavailable(retrieval: Mapping[str, Any]) -> bool:
    """Tell a service failure apart from a genuine zero-hit search.

    An empty channel list alone is *not* a failure: ``SkillSearchService.search``
    returns as soon as nothing ranked, before it propagates ``channels``, so a
    successful search that found nothing also serializes ``channels: []``.
    The warning is what separates the two: every service failure (no
    projection: ``retrieval_unavailable``; Milvus search failed:
    ``vector_unavailable``/``keyword_unavailable``) is reported with one of
    ``SKILL_SEARCH_FAILURE_WARNINGS``.  An empty-channel answer is therefore
    rejected only when it carries a failure marker; empty channels with no
    marker are "searched and found nothing" and stay observable.  A search that
    named a channel is a real search even when it degraded, so it is never
    rejected here.
    """

    if _strings(retrieval.get("channels")):
        return False
    return bool(SKILL_SEARCH_FAILURE_WARNINGS.intersection(_strings(retrieval.get("warnings"))))


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
    "CATALOG_UNAVAILABLE_WARNING",
    "KEYWORD_UNAVAILABLE_WARNING",
    "KNOWLEDGE_SEARCH_TOOL_NAME",
    "RETRIEVAL_TOOL_EVENTS",
    "RETRIEVAL_UNAVAILABLE_WARNING",
    "RetrievalEvent",
    "SEARCH_SKILLS_TOOL_NAME",
    "SKILL_SEARCH_FAILURE_WARNINGS",
    "TOOL_SEARCH_TOOL_NAME",
    "UNAVAILABLE_RETRIEVAL_INDEX",
    "VECTOR_UNAVAILABLE_WARNING",
    "retrieval_event_for_tool",
]
