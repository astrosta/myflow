"""Task 8: semantic confidence thresholds, pending review, and promotion."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.proposals import (
    SemanticDocument,
    SemanticExtraction,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.semantic import SemanticService
from agentkit.config.memory_config import MemoryConfig
from agentkit.agents.memory.types import utc_now
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def semantic_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "s.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    return repo, SemanticService(repo, FakeOperators(), MemoryConfig()), None


def semantic_doc(content: str, *, tier: str = "EXPLICIT_STATEMENT", topic: str = "python.test") -> SemanticExtraction:
    return SemanticExtraction(
        id=f"extract-{content}",
        document=SemanticDocument(
            content=content,
            topic_key=topic,
            evidence_quote=content,
            evidence_tier=tier,
        ),
    )


def test_semantic_thresholds_and_pending_is_not_projected(tmp_path) -> None:
    async def scenario():
        repo, service, _ = await semantic_stack(tmp_path)
        service._operators.semantic_results = [
            semantic_doc("用户偏好 pytest", tier="EXPLICIT_STATEMENT"),
            semantic_doc("用户可能喜欢 unittest", tier="BEHAVIORAL_OBSERVED"),
        ]
        result = await service.ingest_episodes("u1", ["e1"], source_text="用户偏好 pytest；用户可能喜欢 unittest")
        assert [item.status for item in result.records] == ["active", "pending_review"]
        assert len(await repo.list_outbox("u1")) == 1
    run(scenario())


def test_pending_promotes_after_new_evidence(tmp_path) -> None:
    async def scenario():
        repo, service, _ = await semantic_stack(tmp_path)
        service._operators.semantic_results = [semantic_doc("用户偏好 pytest", tier="BEHAVIORAL_OBSERVED")]
        first = await service.ingest_episodes("u1", ["e1"], source_text="用户偏好 pytest")
        pending = first.records[0]
        assert pending.status == "pending_review"
        service._operators.semantic_results = [semantic_doc("用户偏好 pytest", tier="EXPLICIT_STATEMENT")]
        second = await service.ingest_episodes("u1", ["e2"], source_text="用户偏好 pytest")
        assert second.promoted_ids == [pending.id]
        current = await repo.get_record("u1", pending.id)
        assert current is not None and current.status == "active"
    run(scenario())


def test_pending_expires_and_conflict_holds(tmp_path) -> None:
    async def scenario():
        repo, service, _ = await semantic_stack(tmp_path)
        service._operators.semantic_results = [semantic_doc("用户偏好 unittest", tier="BEHAVIORAL_OBSERVED")]
        first = await service.ingest_episodes("u1", ["e1"], source_text="用户偏好 unittest")
        pending = first.records[0]
        assert await service.expire_pending(now=utc_now() + timedelta(days=31)) == 1
        expired = await repo.get_record("u1", pending.id)
        assert expired is not None and expired.status == "expired"

        service._operators.semantic_results = [semantic_doc("用户偏好 pytest", tier="EXPLICIT_STATEMENT")]
        active = await service.ingest_episodes("u1", ["e2"], source_text="用户偏好 pytest")
        assert active.records[0].status == "active"
        service._operators.semantic_results = [semantic_doc("用户偏好 unittest", tier="EXPLICIT_STATEMENT")]
        conflict = await service.ingest_episodes("u1", ["e3"], source_text="用户偏好 unittest")
        assert conflict.conflict_ids
        current = await repo.get_record("u1", active.records[0].id)
        assert current is not None and current.status == "active"
    run(scenario())
