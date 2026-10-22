"""Knowledge metadata store: user isolation, status transitions, counters."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from agentkit.knowledge.errors import KnowledgeDocumentNotFound, KnowledgeNameTaken
from agentkit.knowledge.models import KnowledgeBase, KnowledgeDocument
from agentkit.knowledge.store.memory import MemoryKnowledgeStore
from agentkit.knowledge.store.sql import SqlKnowledgeStore
from agentkit.knowledge.types import KnowledgeBaseType
from agentkit.persistence.base import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def _sql_store(tmp_path: Path) -> tuple[SqlKnowledgeStore, object]:
    import agentkit.knowledge.store.model  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "kb.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return SqlKnowledgeStore(async_sessionmaker(engine, expire_on_commit=False)), engine


def _run(coro):
    return asyncio.run(coro)


async def _exercise(store) -> None:
    base = await store.create_base(
        user_id="u1",
        name="产品文档",
        knowledge_type=KnowledgeBaseType.TECHNICAL,
        description="demo",
    )
    assert isinstance(base, KnowledgeBase)
    assert base.document_count == 0 and base.chunk_count == 0

    with pytest.raises(KnowledgeNameTaken):
        await store.create_base(
            user_id="u1", name="产品文档", knowledge_type=KnowledgeBaseType.FAQ, description=None
        )

    document = await store.create_document(
        document_id="d1",
        user_id="u1",
        knowledge_base_id=base.id,
        filename="manual.pdf",
        stored_name="d1.pdf",
        mime_type="application/pdf",
        size_bytes=12,
    )
    assert isinstance(document, KnowledgeDocument)
    assert (await store.get_base("u1", base.id)).document_count == 1

    ready = await store.update_document_status(
        "u1", "d1", status="ready", chunk_count=3, error_message=None
    )
    assert ready.status == "ready" and ready.chunk_count == 3
    refreshed = await store.get_base("u1", base.id)
    assert refreshed.chunk_count == 3

    queued = await store.prepare_reindex("u1", "d1")
    assert queued.status == "queued" and queued.chunk_count == 0
    assert (await store.get_base("u1", base.id)).chunk_count == 0

    failed = await store.update_document_status(
        "u1", "d1", status="failed", chunk_count=0, error_message="no text"
    )
    assert failed.error_message == "no text"

    deleted = await store.delete_document("u1", "d1")
    assert deleted is not None
    after = await store.get_base("u1", base.id)
    assert after.document_count == 0 and after.chunk_count == 0

    with pytest.raises(KnowledgeDocumentNotFound):
        await store.delete_document("u1", "missing")

    # The same base id cannot be read or mutated through another user.
    assert await store.get_base("u2", base.id) is None
    assert await store.list_bases("u2") == []


def test_sql_store_counter_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, engine = await _sql_store(tmp_path)
        try:
            await _exercise(store)
        finally:
            await engine.dispose()

    _run(scenario())


def test_memory_store_counter_lifecycle() -> None:
    async def scenario() -> None:
        await _exercise(MemoryKnowledgeStore())

    _run(scenario())


def test_recount_repairs_drift(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, engine = await _sql_store(tmp_path)
        try:
            base = await store.create_base(
                user_id="u1", name="FAQ", knowledge_type=KnowledgeBaseType.FAQ, description=None
            )
            await store.create_document(
                document_id="d1", user_id="u1", knowledge_base_id=base.id,
                filename="faq.md", stored_name="d1.md", mime_type="text/markdown", size_bytes=1,
            )
            await store.update_document_status("u1", "d1", status="ready", chunk_count=5)
            # Simulate an old bug or crash: undercount the aggregate.
            async with store._session_factory() as session:
                from agentkit.knowledge.store.model import KnowledgeBaseRow
                from sqlalchemy import update

                await session.execute(
                    update(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == base.id)
                    .values(document_count=0, chunk_count=0)
                )
                await session.commit()
            repaired = await store.recount_base("u1", base.id)
            assert repaired.document_count == 1 and repaired.chunk_count == 5
        finally:
            await engine.dispose()

    _run(scenario())


def test_delete_base_is_scoped_and_removes_documents(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, engine = await _sql_store(tmp_path)
        try:
            base = await store.create_base(
                user_id="u1", name="API", knowledge_type=KnowledgeBaseType.API, description=None
            )
            nested = base.id
            await store.create_document(
                document_id="d1", user_id="u1", knowledge_base_id=nested,
                filename="api.md", stored_name="d1.md", mime_type="text/markdown", size_bytes=1,
            )
            assert await store.list_documents("u1", nested)
            assert await store.delete_base("u2", nested) is False
            assert await store.delete_base("u1", nested) is True
            assert await store.list_documents("u1", nested) == []
            assert await store.get_base("u1", nested) is None
        finally:
            await engine.dispose()

    _run(scenario())
