"""Task 10: hybrid retrieval, PG confirmation, quotas, and fallbacks."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import MutationContext, RecordDraft
from agentkit.agents.memory.canonical.fakes import FakeEmbedder
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.retrieval import HybridRetriever
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


class FakeProjection:
    def __init__(self, hits=None, fail=False):
        self.hits = hits or []
        self.fail = fail

    async def search(self, **kwargs):
        del kwargs
        if self.fail:
            raise RuntimeError("milvus down")
        return self.hits


async def retrieval_stack(tmp_path, *, hits=None, fail=False):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "r.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    config = MemoryConfig()
    retriever = HybridRetriever(repo, FakeProjection(hits, fail), FakeEmbedder(8), config)
    return repo, retriever, engine


def draft(record_id: str, *, kind="semantic", status="active", content="用户偏好 pytest") -> RecordDraft:
    return RecordDraft(
        user_id="u1",
        id=record_id,
        kind=kind,
        subtype="fact" if kind == "semantic" else "episode",
        status=status,
        content=content,
        payload={"content": content},
        topic_key="python.test",
        scope_condition="global",
        source="conversation",
        evidence_tier="EXPLICIT_STATEMENT",
        confidence=0.85 if status == "active" else 0.70,
        salience=0.5,
        explicit_remember=False,
        parent_memory_id=None,
        correction_of=None,
        valid_from=None,
        valid_to=None,
        occurred_at=None,
        thread_id="t1",
        agent_name=None,
        provenance={},
    )


def test_milvus_down_falls_back_to_pg_lexical(tmp_path) -> None:
    async def scenario():
        repo, retriever, engine = await retrieval_stack(tmp_path, fail=True)
        try:
            await repo.create_record(
                draft("m1"), mutation=MutationContext("SYSTEM", "w", "ADD", None, ())
            )
            page = await retriever.search("u1", text="pytest", kinds=["semantic"], limit=8)
            assert [hit.record.id for hit in page.items] == ["m1"]
        finally:
            await engine.dispose()

    run(scenario())


def test_pg_confirmation_drops_cross_user_and_deleted_hits(tmp_path) -> None:
    async def scenario():
        repo, retriever, engine = await retrieval_stack(tmp_path)
        try:
            alive = await repo.create_record(
                draft("m1"), mutation=MutationContext("SYSTEM", "w", "ADD", None, ())
            )
            deleted = await repo.create_record(
                draft("m2"), mutation=MutationContext("SYSTEM", "w", "ADD", None, ())
            )
            await repo.soft_delete(
                "u1", deleted.id, reason="user_forget",
                mutation=MutationContext("USER", "u1", "DELETE", "user_forget", ()),
            )
            retriever._projection.hits = [
                {"pk": "u2:m9", "memory_id": "m9", "score": 0.99, "indexed_revision": 0},
                {"pk": f"u1:{deleted.id}", "memory_id": deleted.id, "score": 0.98, "indexed_revision": 0},
                {"pk": f"u1:{alive.id}", "memory_id": alive.id, "score": 0.90, "indexed_revision": 0},
            ]
            page = await retriever.search("u1", text="pytest", kinds=["semantic"], limit=8)
            assert [hit.record.id for hit in page.items] == [alive.id]
        finally:
            await engine.dispose()

    run(scenario())


def test_search_excludes_pending_and_profile(tmp_path) -> None:
    async def scenario():
        repo, retriever, engine = await retrieval_stack(tmp_path)
        try:
            await repo.create_record(
                draft("p1", status="pending_review"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            page = await retriever.search("u1", text="pytest", kinds=["semantic", "episodic"], limit=8)
            assert page.items == []
        finally:
            await engine.dispose()

    run(scenario())


def test_per_kind_quota_keeps_episodic_bounded(tmp_path) -> None:
    async def scenario():
        repo, retriever, engine = await retrieval_stack(tmp_path)
        try:
            for index in range(10):
                await repo.create_record(
                    draft(f"s{index}"),
                    mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
                )
                await repo.create_record(
                    draft(f"e{index}", kind="episodic"),
                    mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
                )
            page = await retriever.search(
                "u1", text="pytest", kinds=["semantic", "episodic"], limit=20
            )
            assert len([hit for hit in page.items if hit.record.kind == "semantic"]) <= 6
            assert len([hit for hit in page.items if hit.record.kind == "episodic"]) <= 2
        finally:
            await engine.dispose()

    run(scenario())
