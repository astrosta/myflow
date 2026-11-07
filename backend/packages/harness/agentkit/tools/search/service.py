"""Hybrid tool retrieval: Milvus recall, snapshot confirmation, rerank, BM25 fallback."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.catalog import ToolCatalogEntry
from agentkit.contextforge.ownership import can_call
from agentkit.retrieval.bm25 import Bm25Document, bm25_scores
from agentkit.retrieval.tokenize import tokenize

logger = logging.getLogger(__name__)

VECTOR_UNAVAILABLE = "vector_unavailable"
RERANK_UNAVAILABLE = "rerank_unavailable"
CATALOG_UNAVAILABLE = "catalog_unavailable"


class Embedder(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    async def rerank(self, *, query: str, documents: Sequence[str]) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ToolSearchHit:
    entry: ToolCatalogEntry
    score: float


@dataclass(frozen=True, slots=True)
class ToolSearchCard:
    """One ContextForge tool card: the authoritative DB row, ready to serialize.

    The Milvus collection stores no schema, so every field here comes from the
    ``contextforge_tools`` row the hit's ``tool_id`` was joined to.  ``score`` is
    the retrieval score; ``input_schema`` is verbatim unless it exceeded the card
    budget, in which case ``schema_truncated`` is True and it is still valid JSON.
    """

    tool_id: str
    tool_name: str
    call_name: str
    description: str
    tags: tuple[str, ...]
    parameters: tuple[str, ...]
    input_schema: dict[str, Any]
    score: float
    schema_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ToolSearchOutcome:
    """What one retrieval produced.

    ``hits`` carries :class:`ToolSearchHit` for the legacy snapshot catalog and
    :class:`ToolSearchCard` for :class:`ContextForgeToolSearchService`; a caller
    only ever sees the shape of the service it asked.
    """

    hits: tuple[ToolSearchHit | ToolSearchCard, ...] = ()
    channels: tuple[str, ...] = ()
    candidates: int = 0
    reranked: bool = False
    warnings: tuple[str, ...] = ()
    index_kind: str = "local_bm25"


class ToolSearchService:
    def __init__(
        self,
        *,
        index: Any | None,
        embedder: Embedder | None,
        reranker: Reranker | None,
        config: ToolSearchConfig,
        snapshot_provider: Callable[[], Sequence[ToolCatalogEntry]],
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._reranker = reranker
        self._config = config
        self._snapshot_provider = snapshot_provider

    async def search(
        self,
        *,
        task_query: str,
        domain_keywords: Sequence[str] = (),
        user_id: str | None = None,
    ) -> ToolSearchOutcome:
        query = (task_query or "").strip()
        entries = [
            entry for entry in self._snapshot_provider() if can_call(entry.owner, user_id)
        ]
        if not query or not entries:
            return ToolSearchOutcome(channels=self._channels())
        warnings: list[str] = []
        by_name = {entry.name: entry for entry in entries}

        if self._index is not None and self._embedder is not None and self._config.embedding.enabled:
            try:
                vector = await self._embedder.embed_query(query)
                if self._config.keyword.enabled and hasattr(self._index, "hybrid_search"):
                    hits = await self._index.hybrid_search(
                        vector=vector,
                        query=query,
                        limit=self._config.candidate_k,
                    )
                    channels = ("embedding", "keyword")
                else:
                    hits = await self._index.dense_search(
                        vector=vector,
                        limit=self._config.candidate_k,
                    )
                    channels = ("embedding",)
                confirmed = [
                    (by_name[hit.tool_name], float(hit.score))
                    for hit in hits
                    if hit.tool_name in by_name
                ]
                if confirmed:
                    return await self._finish(
                        confirmed,
                        query=query,
                        channels=channels,
                        warnings=warnings,
                        index_kind="milvus",
                        normalize=True,
                    )
            except Exception as exc:  # noqa: BLE001 - local BM25 still answers
                logger.warning("Tool vector search failed: %s", type(exc).__name__)
                warnings.append(VECTOR_UNAVAILABLE)

        return await self._local_bm25(
            entries,
            query=query,
            domain_keywords=domain_keywords,
            warnings=warnings,
        )

    async def _local_bm25(
        self,
        entries: Sequence[ToolCatalogEntry],
        *,
        query: str,
        domain_keywords: Sequence[str],
        warnings: list[str],
    ) -> ToolSearchOutcome:
        if not self._config.keyword.enabled:
            return ToolSearchOutcome(warnings=tuple(warnings), index_kind="unavailable")
        query_terms: Counter[str] = Counter(tokenize(query))
        for keyword in domain_keywords:
            for token in tokenize(keyword):
                query_terms[token] += 2.0
        documents: list[Bm25Document] = []
        for entry in entries:
            tokens = tokenize(entry.content)
            documents.append(
                Bm25Document(
                    key=entry.name,
                    counts=Counter(tokens),
                    length=len(tokens),
                    head_tokens=frozenset(tokenize(f"{entry.name} {entry.display_name}")),
                )
            )
        scores = bm25_scores(
            documents,
            query_terms=query_terms,
            k1=self._config.keyword.bm25_k1,
            b=self._config.keyword.bm25_b,
            name_weight=3.0,
        )
        by_name = {entry.name: entry for entry in entries}
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        ranked = ranked[: self._config.candidate_k]
        confirmed = [(by_name[name], score) for name, score in ranked if name in by_name]
        if not confirmed:
            return ToolSearchOutcome(warnings=tuple(dict.fromkeys(warnings)), index_kind="local_bm25")
        return await self._finish(
            confirmed,
            query=query,
            channels=("keyword",),
            warnings=warnings,
            index_kind="local_bm25",
            normalize=False,
        )

    async def _finish(
        self,
        confirmed: Sequence[tuple[ToolCatalogEntry, float]],
        *,
        query: str,
        channels: Sequence[str],
        warnings: list[str],
        index_kind: str,
        normalize: bool,
    ) -> ToolSearchOutcome:
        ordered = list(confirmed)
        if normalize:
            ceiling = max((score for _, score in ordered), default=0.0)
            if ceiling > 0:
                ordered = [(entry, score / ceiling) for entry, score in ordered]
            if self._config.min_score is not None:
                ordered = [
                    (entry, score)
                    for entry, score in ordered
                    if score >= self._config.min_score
                ]
        candidates = len(ordered)
        reranked = False
        if self._reranker is not None and len(ordered) >= 2:
            try:
                results = await self._reranker.rerank(
                    query=query,
                    documents=[_document(entry) for entry, _ in ordered],
                )
                order: list[int] = []
                for item in results:
                    index = getattr(item, "index", None)
                    if isinstance(index, int) and 0 <= index < len(ordered) and index not in order:
                        order.append(index)
                if order:
                    order += [index for index in range(len(ordered)) if index not in order]
                    ordered = [ordered[index] for index in order]
                    reranked = True
                else:
                    warnings.append(RERANK_UNAVAILABLE)
            except Exception as exc:  # noqa: BLE001 - fused order still answers
                logger.warning("Tool rerank failed: %s", type(exc).__name__)
                warnings.append(RERANK_UNAVAILABLE)
        hits = tuple(
            ToolSearchHit(entry=entry, score=round(float(score), 4))
            for entry, score in ordered[: self._config.top_k]
        )
        return ToolSearchOutcome(
            hits=hits,
            channels=tuple(channels),
            candidates=candidates,
            reranked=reranked,
            warnings=tuple(dict.fromkeys(warnings)),
            index_kind=index_kind,
        )

    def _channels(self) -> tuple[str, ...]:
        channels: list[str] = []
        if self._config.keyword.enabled:
            channels.append("keyword")
        if self._config.embedding.enabled:
            channels.append("embedding")
        return tuple(channels)


def _document(entry: ToolCatalogEntry) -> str:
    return "\n".join(
        part
        for part in (entry.display_name, entry.description, ", ".join(entry.tags))
        if part
    )


# --------------------------------------------------------------------------- #
# ContextForgeToolSearchService: Milvus recall -> DB confirmation -> cards
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
    verbatim source, and the model must see exactly what ContextForge published.
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


class ContextForgeToolSearchService:
    """tool_search retrieval: Milvus recall, database confirmation, cards.

    contextforge_tools is the authority and the Milvus collection is derived, so
    a search is always two steps: recall owner-scoped tool_id hits, then confirm
    each one through the store, which re-checks the owner and the callable gate
    and hands back the verbatim schema.  A candidate the store does not confirm
    is dropped, never substituted from another user's row.  When Milvus or the
    database is unavailable the result is empty with a catalog_unavailable
    warning: v1 has no full-table scan fallback, because scanning the catalog to
    answer one query is exactly the leak the owner filter exists to prevent.
    """

    def __init__(
        self,
        *,
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
        return max(1, min(int(self._config.top_k), MAX_RETRIEVED_TOOLS))

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
            return ToolSearchOutcome(channels=channels, index_kind="unavailable")
        if self._index is None or self._embedder is None or self._store is None:
            return self._unavailable(channels)
        warnings: list[str] = []
        try:
            vector = await self._embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 - a tool result, not a run failure
            logger.warning("Tool query embedding failed: %s", type(exc).__name__)
            return self._unavailable(channels)
        text = _keyword_text(query, domain_keywords)
        try:
            if self._config.keyword.enabled:
                hits = await self._index.hybrid_search(
                    vector=vector,
                    query=text,
                    owner_user_id=owner,
                    limit=self._config.candidate_k,
                )
                channels = ("embedding", "keyword")
            else:
                hits = await self._index.dense_search(
                    vector=vector,
                    owner_user_id=owner,
                    limit=self._config.candidate_k,
                )
                channels = ("embedding",)
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
            call_name=str(getattr(record, "call_name", "") or ""),
            description=_clip_text(
                str(getattr(record, "description", "") or ""),
                self.max_description_chars,
            ),
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
            index_kind="unavailable",
        )

    def _channels(self) -> tuple[str, ...]:
        channels: list[str] = []
        if self._config.keyword.enabled:
            channels.append("keyword")
        if self._config.embedding.enabled:
            channels.append("embedding")
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


#: Whatever the startup hook published: the legacy snapshot service or the
#: ContextForge one.  The built-in only relies on the shared search contract.
ToolSearchRuntime = ToolSearchService | ContextForgeToolSearchService

_service: ToolSearchRuntime | None = None


def set_tool_search_runtime(service: ToolSearchRuntime | None) -> None:
    global _service
    _service = service


def get_tool_search_runtime() -> ToolSearchRuntime | None:
    return _service


def reset_tool_search_runtime() -> None:
    global _service
    _service = None


__all__ = [
    "CATALOG_UNAVAILABLE",
    "ContextForgeToolSearchService",
    "MAX_KEYWORDS",
    "MAX_RESULT_CHARS",
    "MAX_RETRIEVED_TOOLS",
    "MAX_SCHEMA_CHARS",
    "RERANK_UNAVAILABLE",
    "ToolSearchCard",
    "ToolSearchHit",
    "ToolSearchOutcome",
    "ToolSearchRuntime",
    "ToolSearchService",
    "ToolStore",
    "VECTOR_UNAVAILABLE",
    "get_tool_search_runtime",
    "reset_tool_search_runtime",
    "set_tool_search_runtime",
    "truncate_input_schema",
]
