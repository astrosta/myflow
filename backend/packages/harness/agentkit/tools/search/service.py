"""Tool retrieval: Milvus recall, database confirmation, rerank, tool cards.

`mcp_tools` is the authority and the Milvus collection is derived, so
there is exactly one service here: a hit is an owner-scoped `tool_id` that the
store re-checks before its verbatim schema becomes a card for the model.  The
retired snapshot catalog and its local BM25 fallback are gone with the loader
that fed them; a process with no index reports the search as unavailable instead
of answering from a second, stale source.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.config.tool_search_config import ToolSearchConfig

logger = logging.getLogger(__name__)

RERANK_UNAVAILABLE = "rerank_unavailable"
CATALOG_UNAVAILABLE = "catalog_unavailable"
#: index_kind of an outcome that reports a failure instead of a result.  Callers
#: use it to tell "nothing matched" (a real, empty answer) from "retrieval did not
#: run" - the two must never be confused when writing retrieval state.
UNAVAILABLE_INDEX_KIND = "unavailable"


class Embedder(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    async def rerank(self, *, query: str, documents: Sequence[str]) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ToolSearchCard:
    """One MCP tool card: the authoritative DB row, ready to serialize.

    The Milvus collection stores no schema, so every field here comes from the
    ``mcp_tools`` row the hit's ``tool_id`` was joined to.  ``server_name`` names
    the MCP server, ``tool_name`` is the remote name and ``call_name`` is
    ``mcp__server__tool``.  ``score`` is the retrieval score; ``input_schema`` is
    verbatim unless it exceeded the card budget, in which case
    ``schema_truncated`` is True and it is still valid JSON.
    """

    tool_id: str
    tool_name: str
    server_name: str
    call_name: str
    description: str
    tags: tuple[str, ...]
    parameters: tuple[str, ...]
    input_schema: dict[str, Any]
    score: float
    schema_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ToolSearchOutcome:
    """What one retrieval produced: cards plus how they were found.

    ``channels``, ``candidates``, ``reranked`` and ``warnings`` explain the
    retrieval; an outcome whose ``index_kind`` is :data:`UNAVAILABLE_INDEX_KIND`
    reports a failure to retrieve, not an empty match.
    """

    hits: tuple[ToolSearchCard, ...] = ()
    channels: tuple[str, ...] = ()
    candidates: int = 0
    reranked: bool = False
    warnings: tuple[str, ...] = ()
    index_kind: str = UNAVAILABLE_INDEX_KIND


# --------------------------------------------------------------------------- #
# McpToolSearchService: Milvus recall -> DB confirmation -> cards
# --------------------------------------------------------------------------- #

#: The model sees at most five cards, each schema at most 3000 chars, inside one
#: 16000-char ToolMessage (design 8).  Five schemas x 3000 leaves room for the
#: metadata, and max_retrieved_tools follows top_k, capped here.
MAX_RETRIEVED_TOOLS = 5
MAX_SCHEMA_CHARS = 3000
MAX_RESULT_CHARS = 16000
#: Domain keywords folded into the BM25 half of the hybrid query.
MAX_KEYWORDS = 8

#: Keys whose values are maps of names, not schema keywords: a property called
#: "description" is a parameter name and must survive the prune.
_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "definitions")

#: Presentation keys a truncated schema drops first: the model needs the shape,
#: not the author's prose.  Dropping them keeps most schemas inside the budget
#: without touching a single validation keyword.
_SCHEMA_PRESENTATION_KEYS = frozenset({
    "description",
    "title",
    "examples",
    "example",
    "$comment",
    "deprecated",
    "readOnly",
    "writeOnly",
    "externalDocs",
})


class ToolStore(Protocol):
    """The catalog read tool_search performs: the DB join behind a Milvus hit."""

    async def get_by_ids(
        self, ids: Sequence[str], owner_user_id: str
    ) -> Sequence[Any]: ...


def truncate_input_schema(
    schema: Any, max_chars: int = MAX_SCHEMA_CHARS
) -> tuple[dict[str, Any], bool]:
    """Fit one tool schema into the card budget while keeping valid JSON.

    A schema that already fits is returned untouched: the database row is the
    verbatim source, and the model must see exactly what the MCP server published.
    Over budget, presentation keys go first, then properties are dropped from the
    tail with required trimmed to match, and the last resort is a typed empty
    object.  Every step is still a JSON object, so a card can never carry the
    half-string a naive character cut would produce.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}, True
    if len(_serialize_schema(schema)) <= max_chars:
        return schema, False
    pruned = _prune_schema(schema)
    if len(_serialize_schema(pruned)) <= max_chars:
        return pruned, True
    return _clip_properties(pruned, max_chars), True


class McpToolSearchService:
    """tool_search retrieval: Milvus recall, database confirmation, cards.

    mcp_tools is the authority and the Milvus collection is derived, so
    a search is always two steps: recall owner-scoped tool_id hits, then confirm
    each one through the store, which re-checks the owner and the callable gate
    and hands back the verbatim schema.  A candidate the store does not confirm
    is dropped, never substituted from another user's row.  When Milvus or the
    database is unavailable the result is empty with a catalog_unavailable
    warning: v1 has no full-table scan fallback, because scanning the catalog to
    answer one query is exactly the leak the owner filter exists to prevent.

    The recall channel follows the configuration, not a hardcoded pair:
    embedding + keyword fuse through Milvus hybrid search, embedding alone runs
    the dense channel, and keyword alone runs BM25 over the stored sparse vector
    (`keyword_search`), which needs no query embedding.  The reported
    `channels` are the ones the search actually used.
    """

    def __init__(
        self,
        config: ToolSearchConfig,
        index: Any | None = None,
        store: ToolStore | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._config = config
        self._index = index
        self._store = store
        self._embedder = embedder
        self._reranker = reranker

    @property
    def max_cards(self) -> int:
        return max(
            1,
            min(
                int(self._config.top_k),
                int(getattr(self._config, "max_retrieved_tools", MAX_RETRIEVED_TOOLS)),
                MAX_RETRIEVED_TOOLS,
            ),
        )

    @property
    def max_schema_chars(self) -> int:
        return int(getattr(self._config, "max_schema_chars", MAX_SCHEMA_CHARS))

    @property
    def max_description_chars(self) -> int:
        return int(self._config.max_description_chars)

    @property
    def max_result_chars(self) -> int:
        return int(getattr(self._config, "max_result_chars", MAX_RESULT_CHARS))

    async def search(
        self,
        *,
        task_query: str,
        domain_keywords: Sequence[str] = (),
        user_id: str | None = None,
    ) -> ToolSearchOutcome:
        """Retrieve the caller's tools for one task; never executes anything."""
        query = (task_query or "").strip()
        owner = (user_id or "").strip()
        channels = self._channels()
        if not query or not owner:
            # The owner filter is mandatory (index and store both refuse an
            # unscoped read), so an unresolved user is an empty answer, not an
            # unscoped one.
            return ToolSearchOutcome(
                channels=channels, index_kind=UNAVAILABLE_INDEX_KIND
            )
        if not channels:
            # The config validator refuses a search block with both recall
            # channels off, so this only guards a service built by hand: saying
            # "unavailable" beats inventing a channel it cannot run.
            return self._unavailable(channels)
        embedding = bool(self._config.embedding.enabled)
        keyword = bool(self._config.keyword.enabled)
        if self._index is None or self._store is None:
            return self._unavailable(channels)
        # The embedder is only a prerequisite of the dense half: a keyword-only
        # deployment must keep answering when no embedding service is wired.
        if embedding and self._embedder is None:
            return self._unavailable(channels)
        warnings: list[str] = []
        vector: list[float] | None = None
        if embedding:
            try:
                vector = await self._embedder.embed_query(query)
            except Exception as exc:  # noqa: BLE001 - a tool result, not a run failure
                logger.warning("Tool query embedding failed: %s", type(exc).__name__)
                return self._unavailable(channels)
        text = _keyword_text(query, domain_keywords)
        try:
            if embedding and keyword:
                hits = await self._index.hybrid_search(
                    vector=vector,
                    query=text,
                    owner_user_id=owner,
                    limit=self._config.candidate_k,
                )
            elif embedding:
                hits = await self._index.dense_search(
                    vector=vector,
                    owner_user_id=owner,
                    limit=self._config.candidate_k,
                )
            else:
                # Sparse-only: BM25 over the stored vector, no query embedding.
                hits = await self._index.keyword_search(
                    query=text,
                    owner_user_id=owner,
                    limit=self._config.candidate_k,
                )
        except Exception as exc:  # noqa: BLE001 - no full-scan fallback (design 8)
            logger.warning("Tool vector search failed: %s", type(exc).__name__)
            return self._unavailable(channels)
        scores = {hit.tool_id: float(hit.score) for hit in hits if hit.tool_id}
        if not scores:
            return ToolSearchOutcome(
                channels=channels, candidates=len(hits), index_kind="milvus"
            )
        try:
            records = await self._store.get_by_ids(list(scores), owner)
        except Exception as exc:  # noqa: BLE001 - a tool result, not a run failure
            logger.warning("Tool catalog join failed: %s", type(exc).__name__)
            return self._unavailable(channels)
        ordered: list[tuple[Any, float]] = []
        for record in records:
            tool_id = str(getattr(record, "id", "") or "")
            if tool_id in scores:
                ordered.append((record, scores[tool_id]))
        if not ordered:
            return ToolSearchOutcome(channels=channels, index_kind="milvus")
        ceiling = max((score for _, score in ordered), default=0.0)
        if ceiling > 0:
            ordered = [(record, score / ceiling) for record, score in ordered]
        if self._config.min_score is not None:
            ordered = [
                (record, score)
                for record, score in ordered
                if score >= self._config.min_score
            ]
        candidates = len(ordered)
        if not ordered:
            return ToolSearchOutcome(
                channels=channels, candidates=0, index_kind="milvus"
            )
        reranked = False
        if self._reranker is not None and len(ordered) >= 2:
            try:
                results = await self._reranker.rerank(
                    query=query,
                    documents=[_record_document(record) for record, _ in ordered],
                )
                order: list[int] = []
                for item in results:
                    position = getattr(item, "index", None)
                    if (
                        isinstance(position, int)
                        and 0 <= position < len(ordered)
                        and position not in order
                    ):
                        order.append(position)
                if order:
                    order += [
                        position
                        for position in range(len(ordered))
                        if position not in order
                    ]
                    ordered = [ordered[position] for position in order]
                    reranked = True
                else:
                    warnings.append(RERANK_UNAVAILABLE)
            except Exception as exc:  # noqa: BLE001 - fused order still answers
                logger.warning("Tool rerank failed: %s", type(exc).__name__)
                warnings.append(RERANK_UNAVAILABLE)
        cards = tuple(
            self._card(record, score) for record, score in ordered[: self.max_cards]
        )
        return ToolSearchOutcome(
            hits=cards,
            channels=channels,
            candidates=candidates,
            reranked=reranked,
            warnings=tuple(dict.fromkeys(warnings)),
            index_kind="milvus",
        )

    def _card(self, record: Any, score: float) -> ToolSearchCard:
        schema, truncated = truncate_input_schema(
            getattr(record, "input_schema", None), self.max_schema_chars
        )
        return ToolSearchCard(
            tool_id=str(getattr(record, "id", "") or ""),
            tool_name=str(getattr(record, "name", "") or ""),
            server_name=str(getattr(record, "server_name", "") or ""),
            call_name=str(getattr(record, "call_name", "") or ""),
            description=_clip_text(
                str(getattr(record, "description", "") or ""),
                self.max_description_chars,
            ),
            # McpToolRecord has no tags; cards read the DB row, so this stays
            # empty for MCP tools and only supports duck-typed legacy records.
            tags=tuple(getattr(record, "tags", ()) or ()),
            parameters=tuple(getattr(record, "parameters", ()) or ()),
            input_schema=schema,
            score=round(float(score), 4),
            schema_truncated=truncated,
        )

    def _unavailable(self, channels: Sequence[str]) -> ToolSearchOutcome:
        return ToolSearchOutcome(
            channels=tuple(channels),
            warnings=(CATALOG_UNAVAILABLE,),
            index_kind=UNAVAILABLE_INDEX_KIND,
        )

    def _channels(self) -> tuple[str, ...]:
        """The recall channels this config enables, in payload order.

        Read from both flags, so the channels an outcome reports are the ones
        the search could actually run; an empty tuple means there are none.
        """
        channels: list[str] = []
        if self._config.embedding.enabled:
            channels.append("embedding")
        if self._config.keyword.enabled:
            channels.append("keyword")
        return tuple(channels)


def _serialize_schema(document: Any) -> str:
    # The same serialization the ToolMessage uses, so a schema measured against
    # max_schema_chars is exactly the text the model receives.
    return json.dumps(document, ensure_ascii=False)


def _prune_schema(node: Any) -> Any:
    if isinstance(node, dict):
        pruned: dict[str, Any] = {}
        for key, value in node.items():
            if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
                # These keys are property names, not schema keywords: keep every
                # one of them and prune only the schemas underneath.
                pruned[key] = {
                    name: _prune_schema(sub) for name, sub in value.items()
                }
            elif key in _SCHEMA_PRESENTATION_KEYS:
                continue
            else:
                pruned[key] = _prune_schema(value)
        return pruned
    if isinstance(node, list):
        return [_prune_schema(item) for item in node]
    return node


def _clip_properties(schema: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Drop properties from the tail until it fits; trim required to match."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return _schema_skeleton(schema)
    names = list(properties)
    for keep in range(len(names) - 1, -1, -1):
        candidate = {
            key: value for key, value in schema.items() if key != "properties"
        }
        candidate["properties"] = {
            name: properties[name] for name in names[:keep]
        }
        required = schema.get("required")
        if isinstance(required, list):
            still_required = [
                name for name in required if name in candidate["properties"]
            ]
            if still_required:
                candidate["required"] = still_required
            else:
                candidate.pop("required", None)
        if len(_serialize_schema(candidate)) <= max_chars:
            return candidate
    return _schema_skeleton(schema)


def _schema_skeleton(schema: dict[str, Any]) -> dict[str, Any]:
    kind = schema.get("type")
    return {
        "type": kind if isinstance(kind, str) else "object",
        "properties": {},
    }


def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _record_document(record: Any) -> str:
    return "\n".join(
        part
        for part in (
            str(getattr(record, "display_name", "") or ""),
            str(getattr(record, "description", "") or ""),
            ", ".join(getattr(record, "tags", ()) or ()),
        )
        if part
    )


def _keyword_text(query: str, domain_keywords: Sequence[str]) -> str:
    """The BM25 side of the hybrid query: task intent plus domain keywords."""
    keywords: list[str] = []
    for item in domain_keywords:
        if isinstance(item, str):
            text = item.strip()
            if text and text not in keywords:
                keywords.append(text)
    return " ".join([query, *keywords[:MAX_KEYWORDS]])


#: The process-global service the startup hook publishes and tool_search reads.
#: There is only one implementation: the authoritative catalog plus its derived
#: index.  A process that cannot build it publishes None, so the tool reports
#: retrieval as unavailable instead of falling back to a second source.
_service: McpToolSearchService | None = None


def set_tool_search_runtime(service: McpToolSearchService | None) -> None:
    global _service
    _service = service


def get_tool_search_runtime() -> McpToolSearchService | None:
    return _service


def reset_tool_search_runtime() -> None:
    global _service
    _service = None


__all__ = [
    "CATALOG_UNAVAILABLE",
    "McpToolSearchService",
    "MAX_KEYWORDS",
    "MAX_RESULT_CHARS",
    "MAX_RETRIEVED_TOOLS",
    "MAX_SCHEMA_CHARS",
    "RERANK_UNAVAILABLE",
    "ToolSearchCard",
    "ToolSearchOutcome",
    "ToolStore",
    "UNAVAILABLE_INDEX_KIND",
    "get_tool_search_runtime",
    "reset_tool_search_runtime",
    "set_tool_search_runtime",
    "truncate_input_schema",
]
