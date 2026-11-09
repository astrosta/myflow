"""Structured knowledge citations shared by tools, agents, and the gateway.

The retrieval layer already carries document/chunk provenance. This module turns a
hit into a stable, model-visible citation ID, and validates the markers a model
writes back against the citations that were actually retrieved in that run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from langchain_core.messages import ToolMessage

from agentkit.knowledge.models import SearchHit
from agentkit.utils.messages import message_content_to_text

#: additional_kwargs key used on the lead's final AIMessage and on task tool
#: results. The value is always a list of citation objects.
CITATION_KEY = "knowledge_citations"

#: Markers are deliberately narrow: '[' + 'k' + eight lowercase hex chars + ']'.
#: It is stable across turns and distinct from ordinary Markdown links.
CITATION_MARKER_RE = re.compile(r"\[(k[0-9a-f]{8})\]")

_QUOTE_MAX_CHARS = 240


def citation_id_for(
    *, chunk_id: str, document_id: str, knowledge_base_id: str
) -> str:
    """A short, stable ID for one retrieved chunk.

    chunk_id is already document_id:index today, but including the document and
    knowledge-base IDs keeps the contract true even if a vector store starts
    reusing chunk-local IDs.
    """

    material = "\x1f".join((knowledge_base_id, document_id, chunk_id))
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
    return "k" + digest


def citation_for_hit(hit: SearchHit) -> dict[str, Any]:
    """Wire-safe citation metadata. Never includes the full chunk body."""

    return {
        "citation_id": citation_id_for(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            knowledge_base_id=hit.knowledge_base_id,
        ),
        "chunk_id": hit.chunk_id,
        "document_id": hit.document_id,
        "knowledge_base_id": hit.knowledge_base_id,
        "knowledge_base_name": hit.knowledge_base_name,
        "knowledge_base_type": hit.knowledge_base_type,
        "filename": hit.filename,
        "section": hit.section,
        "page": int(hit.page or 0),
        "chunk_index": int(hit.chunk_index or 0),
        "quote": (hit.content or "")[:_QUOTE_MAX_CHARS],
    }


def citations_for_hits(hits: Iterable[SearchHit]) -> list[dict[str, Any]]:
    """Build one manifest, de-duplicated by citation ID and in hit order."""

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        citation = citation_for_hit(hit)
        cid = citation["citation_id"]
        if cid in seen:
            continue
        seen.add(cid)
        output.append(citation)
    return output


def citations_from_search_payload(payload: object) -> list[dict[str, Any]]:
    """Read a knowledge_search JSON payload produced by the tool.

    Malformed entries are ignored rather than failing the whole turn: an old
    tool result or a partial stream must not erase otherwise valid sources.
    """

    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("sources")
    if not isinstance(raw, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        citation = normalize_citation(item)
        if citation is None or citation["citation_id"] in seen:
            continue
        seen.add(citation["citation_id"])
        output.append(citation)
    return output


def normalize_citation(value: object) -> dict[str, Any] | None:
    """Validate one citation object. Missing required fields -> None."""

    if not isinstance(value, Mapping):
        return None
    citation_id = _text(value.get("citation_id"))
    chunk_id = _text(value.get("chunk_id"))
    document_id = _text(value.get("document_id"))
    if not citation_id or not chunk_id or not document_id:
        return None
    if CITATION_MARKER_RE.fullmatch(f"[{citation_id}]") is None:
        return None
    return {
        "citation_id": citation_id,
        "chunk_id": chunk_id,
        "document_id": document_id,
        "knowledge_base_id": _text(value.get("knowledge_base_id")),
        "knowledge_base_name": _text(value.get("knowledge_base_name")),
        "knowledge_base_type": _text(value.get("knowledge_base_type")),
        "filename": _text(value.get("filename")),
        "section": _text(value.get("section")),
        "page": _non_negative_int(value.get("page")),
        "chunk_index": _non_negative_int(value.get("chunk_index")),
        "quote": _text(value.get("quote"))[:_QUOTE_MAX_CHARS],
    }


def resolve_citations(
    text: str, known: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return only citations the model cited and that were actually retrieved.

    First-seen order is preserved. Unknown IDs (hallucinated or stale) are
    dropped, and repeated markers collapse to one entry.
    """

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in CITATION_MARKER_RE.finditer(text):
        cid = match.group(1)
        if cid in seen or cid not in known:
            continue
        seen.add(cid)
        citation = normalize_citation(known[cid])
        if citation is not None:
            output.append(citation)
    return output


def collect_known_citations(messages: Sequence[object]) -> dict[str, dict[str, Any]]:
    """Collect every citation that appeared in tool results in this agent state.

    Two producers are supported:
    * task results put a manifest in additional_kwargs.knowledge_citations;
    * direct knowledge_search results carry a sources array in their JSON.
    """

    known: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(kwargs, Mapping):
            raw = kwargs.get(CITATION_KEY)
            if isinstance(raw, list):
                for item in raw:
                    _remember(known, normalize_citation(item))
        for citation in citations_from_search_payload(_tool_payload(message)):
            _remember(known, citation)
    return known


def citation_ids_in(text: str) -> list[str]:
    """Markers in first-seen order; useful for diagnostics and tests."""

    output: list[str] = []
    seen: set[str] = set()
    for match in CITATION_MARKER_RE.finditer(text):
        cid = match.group(1)
        if cid not in seen:
            seen.add(cid)
            output.append(cid)
    return output


def _tool_payload(message: ToolMessage) -> object:
    content = getattr(message, "content", "")
    if isinstance(content, Mapping):
        return content
    text = message_content_to_text(content)
    if '"sources"' not in text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _remember(known: dict[str, dict[str, Any]], citation: dict[str, Any] | None) -> None:
    if citation is not None:
        known.setdefault(citation["citation_id"], citation)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


__all__ = [
    "CITATION_KEY",
    "CITATION_MARKER_RE",
    "citation_for_hit",
    "citation_id_for",
    "citation_ids_in",
    "citations_for_hits",
    "citations_from_search_payload",
    "collect_known_citations",
    "normalize_citation",
    "resolve_citations",
]
