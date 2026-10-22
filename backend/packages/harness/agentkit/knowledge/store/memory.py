"""In-process knowledge metadata store (database.backend=memory and tests)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from agentkit.knowledge.errors import (
    KnowledgeBaseNotFound,
    KnowledgeDocumentNotFound,
    KnowledgeNameTaken,
)
from agentkit.knowledge.models import KnowledgeBase, KnowledgeDocument
from agentkit.knowledge.store.base import KnowledgeStore
from agentkit.knowledge.types import KnowledgeBaseType
from agentkit.persistence.base import now_iso


def _stamp() -> datetime:
    return datetime.now(UTC)


def _copy_base(item: KnowledgeBase) -> KnowledgeBase:
    return replace(item)


def _copy_doc(item: KnowledgeDocument) -> KnowledgeDocument:
    return replace(item)


class MemoryKnowledgeStore(KnowledgeStore):
    def __init__(self) -> None:
        self._bases: dict[str, KnowledgeBase] = {}
        self._documents: dict[str, KnowledgeDocument] = {}
        self._lock = asyncio.Lock()

    async def create_base(
        self, *, user_id: str, name: str, knowledge_type: KnowledgeBaseType, description: str | None
    ) -> KnowledgeBase:
        async with self._lock:
            if any(item.user_id == user_id and item.name == name for item in self._bases.values()):
                raise KnowledgeNameTaken("A knowledge base with this name already exists")
            stamp = now_iso()
            item = KnowledgeBase(
                id=uuid4().hex, user_id=user_id, name=name, type=knowledge_type.value,
                description=description, document_count=0, chunk_count=0,
                created_at=stamp, updated_at=stamp,
            )
            self._bases[item.id] = item
            return _copy_base(item)

    async def list_bases(self, user_id: str) -> list[KnowledgeBase]:
        return sorted(
            (_copy_base(item) for item in self._bases.values() if item.user_id == user_id),
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )

    async def get_base(self, user_id: str, base_id: str) -> KnowledgeBase | None:
        item = self._bases.get(base_id)
        return _copy_base(item) if item is not None and item.user_id == user_id else None

    async def count_bases(self, user_id: str) -> int:
        return sum(1 for item in self._bases.values() if item.user_id == user_id)

    async def update_base(
        self, *, user_id: str, base_id: str, name: str | None, description: str | None
    ) -> KnowledgeBase:
        async with self._lock:
            item = self._bases.get(base_id)
            if item is None or item.user_id != user_id:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            if name is not None:
                if any(
                    other.user_id == user_id and other.name == name and other.id != base_id
                    for other in self._bases.values()
                ):
                    raise KnowledgeNameTaken("A knowledge base with this name already exists")
                item.name = name
            if description is not None:
                item.description = description
            item.updated_at = now_iso()
            return _copy_base(item)

    async def delete_base(self, user_id: str, base_id: str) -> bool:
        async with self._lock:
            item = self._bases.get(base_id)
            if item is None or item.user_id != user_id:
                return False
            del self._bases[base_id]
            for doc_id in [key for key, doc in self._documents.items() if doc.knowledge_base_id == base_id]:
                del self._documents[doc_id]
            return True

    async def create_document(
        self, *, document_id: str, user_id: str, knowledge_base_id: str, filename: str,
        stored_name: str, mime_type: str, size_bytes: int,
    ) -> KnowledgeDocument:
        async with self._lock:
            base = self._bases.get(knowledge_base_id)
            if base is None or base.user_id != user_id:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            stamp = now_iso()
            item = KnowledgeDocument(
                id=document_id, user_id=user_id, knowledge_base_id=knowledge_base_id,
                filename=filename, stored_name=stored_name, mime_type=mime_type,
                size_bytes=size_bytes, status="queued", chunk_count=0,
                error_message=None, created_at=stamp, updated_at=stamp,
            )
            self._documents[document_id] = item
            base.document_count += 1
            base.updated_at = stamp
            return _copy_doc(item)

    async def list_documents(self, user_id: str, base_id: str) -> list[KnowledgeDocument]:
        return sorted(
            (
                _copy_doc(item) for item in self._documents.values()
                if item.user_id == user_id and item.knowledge_base_id == base_id
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    async def get_document(self, user_id: str, document_id: str) -> KnowledgeDocument | None:
        item = self._documents.get(document_id)
        return _copy_doc(item) if item is not None and item.user_id == user_id else None

    async def count_documents(self, user_id: str, base_id: str) -> int:
        return sum(
            1 for item in self._documents.values()
            if item.user_id == user_id and item.knowledge_base_id == base_id
        )

    async def update_document_status(
        self, user_id: str, document_id: str, *, status: str,
        chunk_count: int | None = None, error_message: str | None = None,
    ) -> KnowledgeDocument:
        async with self._lock:
            item = self._documents.get(document_id)
            if item is None or item.user_id != user_id:
                raise KnowledgeDocumentNotFound("Document not found")
            base = self._bases[item.knowledge_base_id]
            old = item.chunk_count
            new = old if chunk_count is None else max(0, chunk_count)
            item.status = status  # type: ignore[assignment]
            item.chunk_count = new
            item.error_message = error_message
            item.updated_at = now_iso()
            base.chunk_count = max(0, base.chunk_count + (new - old))
            base.updated_at = item.updated_at
            return _copy_doc(item)

    async def prepare_reindex(self, user_id: str, document_id: str) -> KnowledgeDocument:
        return await self.update_document_status(
            user_id, document_id, status="queued", chunk_count=0, error_message=None
        )

    async def delete_document(self, user_id: str, document_id: str) -> KnowledgeDocument | None:
        async with self._lock:
            item = self._documents.get(document_id)
            if item is None or item.user_id != user_id:
                raise KnowledgeDocumentNotFound("Document not found")
            base = self._bases[item.knowledge_base_id]
            del self._documents[document_id]
            base.document_count = max(0, base.document_count - 1)
            base.chunk_count = max(0, base.chunk_count - item.chunk_count)
            base.updated_at = now_iso()
            return _copy_doc(item)

    async def list_interrupted(self) -> list[KnowledgeDocument]:
        return [
            _copy_doc(item) for item in self._documents.values()
            if item.status in {"parsing", "chunking", "embedding"}
        ]

    async def mark_interrupted_failed(self) -> int:
        count = 0
        async with self._lock:
            for item in self._documents.values():
                if item.status in {"parsing", "chunking", "embedding"}:
                    item.status = "failed"
                    item.error_message = "服务重启，任务中断，请重新索引"
                    item.updated_at = now_iso()
                    count += 1
        return count

    async def recount_base(self, user_id: str, base_id: str) -> KnowledgeBase:
        async with self._lock:
            base = self._bases.get(base_id)
            if base is None or base.user_id != user_id:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            docs = [item for item in self._documents.values() if item.knowledge_base_id == base_id]
            base.document_count = len(docs)
            base.chunk_count = sum(item.chunk_count for item in docs if item.status == "ready")
            base.updated_at = now_iso()
            return _copy_base(base)
