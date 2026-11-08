"""Hybrid tool retrieval: Milvus recall, snapshot confirmation, rerank.

Both recall channels and the RRF fusion run in Milvus.  The process only embeds
the query, confirms hits against the current tool snapshot, and optionally
reranks the survivors; there is no in-process BM25 or RRF implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.catalog import ToolCatalogEntry
from agentkit.contextforge.ownership import can_call

logger = logging.getLogger(__name__)

VECTOR_UNAVAILABLE = "vector_unavailable"
KEYWORD_UNAVAILABLE = "keyword_unavailable"
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
class ToolSearchOutcome:
    hits: tuple[ToolSearchHit, ...] = ()
    channels: tuple[str, ...] = ()
    candidates: int = 0
    reranked: bool = False
    warnings: tuple[str, ...] = ()
    index_kind: str = "unavailable"


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
        if self._index is None:
            return ToolSearchOutcome(
                channels=self._channels(),
                warnings=(CATALOG_UNAVAILABLE,),
                index_kind="unavailable",
            )

        warnings: list[str] = []
        by_name = {entry.name: entry for entry in entries}
        keyword_query = self._keyword_query(query, domain_keywords)
        vector: list[float] | None = None
        if self._embedder is not None and self._config.embedding.enabled:
            try:
                vector = list(await self._embedder.embed_query(query))
            except Exception as exc:  # noqa: BLE001 - Milvus BM25 can still answer
                logger.warning(
                    "Tool query embedding failed (%s); using Milvus BM25",
                    type(exc).__name__,
                )
                warnings.append(VECTOR_UNAVAILABLE)
        if self._config.embedding.enabled and vector is None and keyword_query:
            warnings.append(VECTOR_UNAVAILABLE)
        if not vector and not keyword_query:
            warnings.append(
                VECTOR_UNAVAILABLE
                if self._config.embedding.enabled
                else KEYWORD_UNAVAILABLE
            )
            return ToolSearchOutcome(
                channels=self._channels(),
                warnings=tuple(dict.fromkeys(warnings)),
                index_kind="unavailable",
            )

        if vector is not None and keyword_query:
            channels = ("embedding", "keyword")
        elif vector is not None:
            channels = ("embedding",)
        else:
            channels = ("keyword",)

        try:
            if vector is not None and keyword_query:
                hits = await self._index.hybrid_search(
                    vector=vector,
                    query=keyword_query,
                    limit=self._config.candidate_k,
                )
            elif vector is not None:
                hits = await self._index.dense_search(
                    vector=vector,
                    limit=self._config.candidate_k,
                )
            else:
                hits = await self._index.keyword_search(
                    query=keyword_query,
                    limit=self._config.candidate_k,
                )
        except Exception as exc:  # noqa: BLE001 - no local BM25 fallback
            logger.warning(
                "Tool Milvus search failed (%s); returning no local hits",
                type(exc).__name__,
            )
            warnings.append(
                KEYWORD_UNAVAILABLE if channels == ("keyword",) else VECTOR_UNAVAILABLE
            )
            return ToolSearchOutcome(
                channels=channels,
                warnings=tuple(dict.fromkeys(warnings)),
                index_kind="milvus",
            )

        confirmed: list[tuple[ToolCatalogEntry, float]] = []
        seen: set[str] = set()
        for hit in hits:
            entry = by_name.get(hit.tool_name)
            if entry is None or hit.tool_name in seen:
                continue
            score = float(hit.score)
            if self._config.min_score is not None and score < self._config.min_score:
                continue
            confirmed.append((entry, score))
            seen.add(hit.tool_name)
        if not confirmed:
            return ToolSearchOutcome(
                channels=channels,
                warnings=tuple(dict.fromkeys(warnings)),
                index_kind="milvus",
            )
        return await self._finish(
            confirmed,
            query=query,
            channels=channels,
            warnings=warnings,
            index_kind="milvus",
        )

    async def _finish(
        self,
        confirmed: Sequence[tuple[ToolCatalogEntry, float]],
        *,
        query: str,
        channels: Sequence[str],
        warnings: list[str],
        index_kind: str,
    ) -> ToolSearchOutcome:
        ordered = list(confirmed)
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

    def _keyword_query(self, query: str, domain_keywords: Sequence[str]) -> str:
        """Build the BM25 text; tokenization and scoring stay in Milvus."""
        if not self._config.keyword.enabled:
            return ""
        parts = [query, *(item.strip() for item in domain_keywords)]
        return " ".join(dict.fromkeys(item for item in parts if item))

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


_service: ToolSearchService | None = None


def set_tool_search_runtime(service: ToolSearchService | None) -> None:
    global _service
    _service = service


def get_tool_search_runtime() -> ToolSearchService | None:
    return _service


def reset_tool_search_runtime() -> None:
    global _service
    _service = None


__all__ = [
    "CATALOG_UNAVAILABLE",
    "KEYWORD_UNAVAILABLE",
    "RERANK_UNAVAILABLE",
    "VECTOR_UNAVAILABLE",
    "ToolSearchHit",
    "ToolSearchOutcome",
    "ToolSearchService",
    "get_tool_search_runtime",
    "reset_tool_search_runtime",
    "set_tool_search_runtime",
]
