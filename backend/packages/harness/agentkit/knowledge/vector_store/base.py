"""Vector store protocol."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from agentkit.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeParentChunk,
    SearchHit,
)


class VectorStore(Protocol):
    async def upsert_document(
        self,
        *,
        user_id: str,
        knowledge_base_id: str,
        knowledge_base_name: str,
        knowledge_base_type: str,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float]],
        parents: Sequence[KnowledgeParentChunk] = (),
    ) -> None: ...

    async def get_parents(
        self, *, user_id: str, parent_ids: Sequence[str]
    ) -> dict[str, str]: ...

    async def list_chunks(
        self, *, user_id: str, document_id: str
    ) -> list[KnowledgeChunk]: ...

    async def delete_document(self, *, user_id: str, document_id: str) -> None: ...

    async def delete_base(self, *, user_id: str, knowledge_base_id: str) -> None: ...

    async def search(
        self,
        *,
        user_id: str,
        vector: Sequence[float],
        base_ids: Sequence[str] | None = None,
        limit: int = 5,
        score_threshold: float | None = None,
    ) -> list[SearchHit]: ...

    async def hybrid_search(
        self,
        *,
        user_id: str,
        vector: Sequence[float],
        keyword_query: str,
        base_ids: Sequence[str] | None = None,
        limit: int = 20,
        rrf_k: int = 60,
    ) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...
