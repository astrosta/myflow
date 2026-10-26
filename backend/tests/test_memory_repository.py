"""Task 3: canonical repository transaction, CAS, ledger, and outbox."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MutationContext,
    RecordDraft,
)
from agentkit.agents.memory.canonical.models import (
    MemoryIndexOutboxRow,
    MemoryMutationLedgerRow,
    MemoryRecordRow,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def _repo(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "m.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return MemoryRepository(async_sessionmaker(engine, expire_on_commit=False)), engine


def semantic_draft(content: str, *, record_id: str = "m1") -> RecordDraft:
    return RecordDraft(
        user_id="u1",
        id=record_id,
        kind="semantic",
        subtype="fact",
        status="active",
        content=content,
        payload={"content": content},
        topic_key="t",
        scope_condition="global",
        source="conversation",
        evidence_tier="EXPLICIT_STATEMENT",
        confidence=0.85,
        salience=0.5,
        explicit_remember=False,
        parent_memory_id=None,
        correction_of=None,
        valid_from=None,
        valid_to=None,
        occurred_at=None,
        thread_id="t1",
        agent_name=None,
        provenance={"message_id": "m1"},
        source_episode_ids=(),
    )


def test_record_write_is_one_atomic_unit(tmp_path) -> None:
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            record = await repo.create_record(
                semantic_draft("用户偏好 pytest"),
                mutation=MutationContext("SYSTEM", "memory-worker", "ADD", None, ("e1",)),
            )
            async with repo._sf() as session:
                assert await session.get(MemoryRecordRow, record.id) is not None
                assert await session.scalar(
                    select(func.count()).select_from(MemoryMutationLedgerRow)
                ) == 1
                assert await session.scalar(
                    select(func.count()).select_from(MemoryIndexOutboxRow)
                ) == 1
        finally:
            await engine.dispose()

    run(scenario())


def test_profile_revision_conflict(tmp_path) -> None:
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            first = await repo.ensure_profile("u1")
            await repo.cas_profile(
                "u1",
                expected_revision=first.revision,
                profile={"identity": {}},
                tombstones={},
                mutation=MutationContext("USER", "u1", "UPDATE", "manual", ()),
            )
            with pytest.raises(MemoryConflictError, match="revision"):
                await repo.cas_profile(
                    "u1",
                    expected_revision=first.revision,
                    profile={"identity": {}},
                    tombstones={},
                    mutation=MutationContext("USER", "u1", "UPDATE", "manual", ()),
                )
        finally:
            await engine.dispose()

    run(scenario())


def test_rollback_creates_a_new_active_revision(tmp_path) -> None:
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            first = await repo.create_record(
                semantic_draft("v1"),
                mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
            )
            second = await repo.update_record(
                "u1",
                first.id,
                expected_revision=first.revision,
                changes={"content": "v2", "payload": {"content": "v2"}},
                mutation=MutationContext("SYSTEM", "w", "UPDATE", None, ()),
            )
            rolled = await repo.rollback(
                "u1",
                first.id,
                target_revision=first.revision,
                mutation=MutationContext("ADMIN", "admin", "ROLLBACK", "manual", ()),
            )
            assert rolled.revision == second.revision + 1
            assert rolled.status == "active"
            assert rolled.content == "v1"
        finally:
            await engine.dispose()

    run(scenario())
