"""Protocols shared by the Milvus-backed skill retrieval stages.

Recall, BM25 scoring, and RRF fusion belong to Milvus (see
`agentkit.skills.projection`).  This module deliberately contains no local
ranking implementation: it only names the optional external services the search
service may call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """bi-encoder client used to produce the dense query vector."""

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """Optional cross-encoder stage; it runs after Milvus recall."""

    async def rerank(self, *, query: str, documents: Sequence[str]) -> Sequence[object]: ...


__all__ = ["Embedder", "Reranker"]
