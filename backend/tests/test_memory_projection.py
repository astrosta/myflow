"""Task 9: outbox projector, Milvus rows, stale-event fencing, reindex."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import MutationContext, RecordDraft
from agentkit.agents.memory.canonical.fakes import FakeEmbedder
from agentkit.agents.memory.canonical.projection import (
    MemoryMilvusProjection,
    MemoryProjector,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


class FakeMilvus:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.filters: list[str] = []

    def has_collection(self, name: str) -> bool:
        return True

    def upsert(self, *, collection_name: str, data: list[dict]) -> None:
        del collection_name
        for row in data:
            self.rows[row["pk"]] = row

    def delete(self, *, collection_name: str, filter: str) -> None:
        del collection_name
        self.filters.append(filter)
        for key in list(self.rows):
            if key in filter or f"pk == '{key}'" in filter or f'pk == "{key}"' in filter:
                self.rows.pop(key, None)

    def search(self, **kwargs):
        self.filters.append(kwargs["filter"])
        return []


async def projection_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "p.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    fake = FakeMilvus()
    config = MemoryConfig()
    projection = MemoryMilvusProjection(config.vector_store, dimensions=8, client=fake)
    projector = MemoryProjector(repo, projection, FakeEmbedder(8), config)
    return repo, projector, fake, engine


def active_episode(record_id: str) -> RecordDraft:
    return RecordDraft(
        user_id="u1",
        id=record_id,
        kind="episodic",
        subtype="episode",
        status="active",
        content="部署修复成功",
        payload={"content": "部署修复成功"},
        topic_key="deploy",
        scope_condition="global",
        source="conversation",
        evidence_tier="BEHAVIORAL_OBSERVED",
        confidence=0.70,
        salience=0.8,
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


def pending_semantic(record_id: str) -> RecordDraft:
    return replace(
        active_episode(record_id),
        kind="semantic",
        subtype="fact",
        status="pending_review",
        content="用户可能喜欢 unittest",
        topic_key="python.test",
        confidence=0.70,
    )


def test_projection_active_only_and_filter(tmp_path) -> None:
    async def scenario():
        repo, projector, fake, engine = await projection_stack(tmp_path)
        try:
            await repo.create_record(
                active_episode("m1"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            await repo.create_record(
                pending_semantic("m2"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            assert await projector.run_once() == 1
            assert set(fake.rows) == {"u1:m1"}
            assert fake.rows["u1:m1"]["status"] == "active"
            await projector.projection.search(
                user_id="u1", kinds=["semantic"], vector=[0.0] * 8, limit=1
            )
            assert 'status == "active"' in fake.filters[-1]
            assert "valid_to == 0" in fake.filters[-1]
        finally:
            await engine.dispose()

    run(scenario())


def test_stale_outbox_after_delete_is_dropped(tmp_path) -> None:
    async def scenario():
        repo, projector, fake, engine = await projection_stack(tmp_path)
        try:
            record = await repo.create_record(
                active_episode("m1"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            await repo.soft_delete(
                "u1",
                record.id,
                reason="user_forget",
                mutation=MutationContext("USER", "u1", "DELETE", "user_forget", ()),
            )
            assert await projector.run_once() == 2
            assert fake.rows == {}
        finally:
            await engine.dispose()

    run(scenario())


def test_reindex_rebuilds_from_pg_active(tmp_path) -> None:
    async def scenario():
        repo, projector, fake, engine = await projection_stack(tmp_path)
        try:
            await repo.create_record(
                active_episode("m1"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            await repo.create_record(
                active_episode("m2"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            await repo.create_record(
                pending_semantic("m3"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            assert await projector.reindex("u1") >= 2
            assert set(fake.rows) == {"u1:m1", "u1:m2"}
        finally:
            await engine.dispose()

    run(scenario())
