"""Hybrid tool retrieval: Milvus recall, snapshot confirmation, rerank, BM25 fallback."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.catalog import ToolCatalogEntry
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
class ToolSearchOutcome:
    hits: tuple[ToolSearchHit, ...] = ()
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
    ) -> ToolSearchOutcome:
        query = (task_query or "").strip()
        entries = list(self._snapshot_provider())
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
    "RERANK_UNAVAILABLE",
    "VECTOR_UNAVAILABLE",
    "ToolSearchHit",
    "ToolSearchOutcome",
    "ToolSearchService",
    "get_tool_search_runtime",
    "reset_tool_search_runtime",
    "set_tool_search_runtime",
]
