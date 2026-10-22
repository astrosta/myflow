"""SQL metadata store. Every query is scoped by the authenticated user."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError

from agentkit.knowledge.errors import (
    KnowledgeBaseNotFound,
    KnowledgeDocumentNotFound,
    KnowledgeNameTaken,
)
from agentkit.knowledge.models import KnowledgeBase, KnowledgeDocument
from agentkit.knowledge.store.base import KnowledgeStore
from agentkit.knowledge.store.model import KnowledgeBaseRow, KnowledgeDocumentRow
from agentkit.knowledge.types import KnowledgeBaseType
from agentkit.persistence.base import iso_utc

logger = logging.getLogger(__name__)


def _base(row: KnowledgeBaseRow) -> KnowledgeBase:
    return KnowledgeBase(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        type=row.type,
        description=row.description,
        document_count=row.document_count,
        chunk_count=row.chunk_count,
        created_at=iso_utc(row.created_at) or "",
        updated_at=iso_utc(row.updated_at) or "",
    )


def _doc(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row.id,
        user_id=row.user_id,
        knowledge_base_id=row.knowledge_base_id,
        filename=row.filename,
        stored_name=row.stored_name,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        status=row.status,  # type: ignore[arg-type]
        chunk_count=row.chunk_count,
        error_message=row.error_message,
        created_at=iso_utc(row.created_at) or "",
        updated_at=iso_utc(row.updated_at) or "",
    )


class SqlKnowledgeStore(KnowledgeStore):
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def create_base(
        self, *, user_id: str, name: str, knowledge_type: KnowledgeBaseType, description: str | None
    ) -> KnowledgeBase:
        async with self._session_factory() as session:
            row = KnowledgeBaseRow(
                id=uuid4().hex,
                user_id=user_id,
                name=name,
                type=knowledge_type.value,
                description=description,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KnowledgeNameTaken("A knowledge base with this name already exists") from exc
            return _base(row)

    async def list_bases(self, user_id: str) -> list[KnowledgeBase]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.user_id == user_id)
                    .order_by(KnowledgeBaseRow.updated_at.desc(), KnowledgeBaseRow.id.desc())
                )
            ).all()
        return [_base(row) for row in rows]

    async def get_base(self, user_id: str, base_id: str) -> KnowledgeBase | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.user_id == user_id
                )
            )
        return _base(row) if row is not None else None

    async def count_bases(self, user_id: str) -> int:
        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count(KnowledgeBaseRow.id)).where(KnowledgeBaseRow.user_id == user_id)
                )
                or 0
            )

    async def update_base(
        self, *, user_id: str, base_id: str, name: str | None, description: str | None
    ) -> KnowledgeBase:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.user_id == user_id
                )
            )
            if row is None:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            if name is not None:
                row.name = name
            if description is not None:
                row.description = description
            row.updated_at = datetime.now(UTC)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise KnowledgeNameTaken("A knowledge base with this name already exists") from exc
            return _base(row)

    async def delete_base(self, user_id: str, base_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.user_id == user_id
                )
            )
            if row is None:
                return False
            await session.execute(
                sa_delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.knowledge_base_id == base_id,
                    KnowledgeDocumentRow.user_id == user_id,
                )
            )
            await session.delete(row)
            await session.commit()
            return True

    async def create_document(
        self, *, document_id: str, user_id: str, knowledge_base_id: str, filename: str,
        stored_name: str, mime_type: str, size_bytes: int,
    ) -> KnowledgeDocument:
        async with self._session_factory() as session:
            base = await session.scalar(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.id == knowledge_base_id, KnowledgeBaseRow.user_id == user_id
                )
            )
            if base is None:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            row = KnowledgeDocumentRow(
                id=document_id,
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                filename=filename,
                stored_name=stored_name,
                mime_type=mime_type,
                size_bytes=size_bytes,
                status="queued",
            )
            session.add(row)
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.id == knowledge_base_id)
                .values(
                    document_count=KnowledgeBaseRow.document_count + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return _doc(row)

    async def list_documents(self, user_id: str, base_id: str) -> list[KnowledgeDocument]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.user_id == user_id,
                        KnowledgeDocumentRow.knowledge_base_id == base_id,
                    )
                    .order_by(KnowledgeDocumentRow.created_at.desc(), KnowledgeDocumentRow.id.desc())
                )
            ).all()
        return [_doc(row) for row in rows]

    async def get_document(self, user_id: str, document_id: str) -> KnowledgeDocument | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.id == document_id,
                    KnowledgeDocumentRow.user_id == user_id,
                )
            )
        return _doc(row) if row is not None else None

    async def count_documents(self, user_id: str, base_id: str) -> int:
        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count(KnowledgeDocumentRow.id)).where(
                        KnowledgeDocumentRow.user_id == user_id,
                        KnowledgeDocumentRow.knowledge_base_id == base_id,
                    )
                )
                or 0
            )

    async def update_document_status(
        self, user_id: str, document_id: str, *, status: str,
        chunk_count: int | None = None, error_message: str | None = None,
    ) -> KnowledgeDocument:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.id == document_id,
                    KnowledgeDocumentRow.user_id == user_id,
                )
            )
            if row is None:
                raise KnowledgeDocumentNotFound("Document not found")
            old_chunks = row.chunk_count
            new_chunks = old_chunks if chunk_count is None else max(0, chunk_count)
            row.status = status
            row.chunk_count = new_chunks
            row.error_message = error_message
            row.updated_at = datetime.now(UTC)
            delta = new_chunks - old_chunks
            if delta:
                await self._adjust_base(
                    session, row.user_id, row.knowledge_base_id,
                    document_delta=0, chunk_delta=delta,
                )
            await session.commit()
            return _doc(row)

    async def prepare_reindex(self, user_id: str, document_id: str) -> KnowledgeDocument:
        return await self.update_document_status(
            user_id, document_id, status="queued", chunk_count=0, error_message=None
        )

    async def delete_document(self, user_id: str, document_id: str) -> KnowledgeDocument | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.id == document_id,
                    KnowledgeDocumentRow.user_id == user_id,
                )
            )
            if row is None:
                raise KnowledgeDocumentNotFound("Document not found")
            snapshot = _doc(row)
            await session.delete(row)
            await self._adjust_base(
                session, row.user_id, row.knowledge_base_id,
                document_delta=-1, chunk_delta=-row.chunk_count,
            )
            await session.commit()
            return snapshot

    async def list_interrupted(self) -> list[KnowledgeDocument]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(KnowledgeDocumentRow).where(
                        KnowledgeDocumentRow.status.in_(("parsing", "chunking", "embedding"))
                    )
                )
            ).all()
        return [_doc(row) for row in rows]

    async def mark_interrupted_failed(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(KnowledgeDocumentRow.status.in_(("parsing", "chunking", "embedding")))
                .values(
                    status="failed",
                    error_message="服务重启，任务中断，请重新索引",
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def recount_base(self, user_id: str, base_id: str) -> KnowledgeBase:
        async with self._session_factory() as session:
            base = await session.scalar(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.user_id == user_id
                )
            )
            if base is None:
                raise KnowledgeBaseNotFound("Knowledge base not found")
            documents = await session.scalar(
                select(func.count(KnowledgeDocumentRow.id)).where(
                    KnowledgeDocumentRow.user_id == user_id,
                    KnowledgeDocumentRow.knowledge_base_id == base_id,
                )
            )
            chunks = await session.scalar(
                select(func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0)).where(
                    KnowledgeDocumentRow.user_id == user_id,
                    KnowledgeDocumentRow.knowledge_base_id == base_id,
                    KnowledgeDocumentRow.status == "ready",
                )
            )
            base.document_count = int(documents or 0)
            base.chunk_count = int(chunks or 0)
            base.updated_at = datetime.now(UTC)
            await session.commit()
            return _base(base)

    async def _adjust_base(
        self, session, user_id: str, base_id: str, *, document_delta: int, chunk_delta: int
    ) -> None:
        document_expr = KnowledgeBaseRow.document_count + document_delta
        chunk_expr = KnowledgeBaseRow.chunk_count + chunk_delta
        await session.execute(
            update(KnowledgeBaseRow)
            .where(KnowledgeBaseRow.id == base_id, KnowledgeBaseRow.user_id == user_id)
            .values(
                document_count=case((document_expr < 0, 0), else_=document_expr),
                chunk_count=case((chunk_expr < 0, 0), else_=chunk_expr),
                updated_at=datetime.now(UTC),
            )
        )
