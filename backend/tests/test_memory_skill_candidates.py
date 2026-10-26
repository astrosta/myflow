"""Task 11: host-gated skill candidates and explicit confirmation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryNotFoundError,
    MutationContext,
    RecordDraft,
)
from agentkit.agents.memory.canonical.fakes import FakeEmbedder, FakeOperators
from agentkit.agents.memory.canonical.proposals import DehydratedEpisode, SkillProposal
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.skill_candidates import SkillCandidateService
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


class FakeSkillAuthor:
    def __init__(self) -> None:
        self.create_calls = 0
        self.put_calls = 0
        self.saved_metadata = None
        self.submit_calls = 0

    async def create_draft(self, *, owner_user_id, name, description):
        del owner_user_id, name, description
        self.create_calls += 1
        return SimpleNamespace(id="skill-1")

    async def put_file(self, *, owner_user_id, skill_id, path, content, mode="text"):
        del owner_user_id, skill_id, path, content, mode
        self.put_calls += 1

    async def save(self, *, owner_user_id, skill_id, extra_metadata=None):
        del owner_user_id, skill_id
        self.saved_metadata = extra_metadata
        return SimpleNamespace(id="version-1")


async def skill_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "k.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    config = MemoryConfig()
    ops = FakeOperators()
    author = FakeSkillAuthor()
    service = SkillCandidateService(repo, ops, FakeEmbedder(8), author, config)
    return repo, service, ops, author, engine


def valid_skill_draft(same_sop: bool = True) -> SkillProposal:
    return SkillProposal(
        markdown="---\nname: deploy-fix\ndescription: 修复部署\n---\n",
        same_sop=same_sop,
    )


async def success_episode(
    repo: MemoryRepository,
    record_id: str,
    *,
    thread: str,
    environment: dict | None = None,
    trigger: str = "修复部署失败",
    steps: list[str] | None = None,
) -> None:
    episode = DehydratedEpisode(
        task_intent="修复部署失败",
        final_outcome="SUCCESS",
        key_takeaway="重启 upstream",
        evidence_refs=["message:a1"],
        task_class="deploy_fix",
        environment=environment or {"os": "linux"},
        trigger_text=trigger,
        key_steps=steps or ["exec", "read"],
        feedback_signal="ACCEPTED",
    )
    await repo.create_record(
        RecordDraft(
            user_id="u1",
            id=record_id,
            kind="episodic",
            subtype="episode",
            status="active",
            content="部署修复",
            payload=episode.model_dump(mode="json"),
            topic_key="deploy_fix",
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
            thread_id=thread,
            agent_name=None,
            provenance={},
        ),
        mutation=MutationContext("SYSTEM", "w", "ADD", None, ()),
    )


def test_three_successful_episodes_create_candidate(tmp_path) -> None:
    async def scenario():
        repo, service, ops, _author, engine = await skill_stack(tmp_path)
        try:
            ops.skill_result = valid_skill_draft()
            for index, thread in enumerate(("t1", "t2", "t2"), start=1):
                await success_episode(repo, f"e{index}", thread=thread)
            candidates = await service.generate("u1")
            assert len(candidates) == 1
            assert candidates[0].status == "pending_confirmation"
            assert candidates[0].source_episode_ids == ["e1", "e2", "e3"]
        finally:
            await engine.dispose()

    run(scenario())


def test_host_environment_gate_beats_d_same_sop(tmp_path) -> None:
    async def scenario():
        repo, service, ops, _author, engine = await skill_stack(tmp_path)
        try:
            ops.skill_result = valid_skill_draft(same_sop=True)
            await success_episode(repo, "e1", thread="t1", environment={"os": "linux"})
            await success_episode(repo, "e2", thread="t2", environment={"os": "windows"})
            await success_episode(repo, "e3", thread="t3", environment={"os": "linux"})
            assert await service.generate("u1") == []
        finally:
            await engine.dispose()

    run(scenario())


def test_explicit_one_episode_and_confirm_is_idempotent(tmp_path) -> None:
    async def scenario():
        repo, service, ops, author, engine = await skill_stack(tmp_path)
        try:
            ops.skill_result = valid_skill_draft()
            await success_episode(repo, "e1", thread="t1")
            candidates = await service.generate(
                "u1", explicit_episode_ids=["e1"], explicit_user_request=True
            )
            assert len(candidates) == 1
            assert author.submit_calls == 0
            candidate = candidates[0]
            result = await service.confirm(
                "u1",
                candidate.id,
                expected_revision=candidate.revision,
                package_sha256=candidate.package_sha256,
                idempotency_key="idem-1",
            )
            assert result.skill_id == "skill-1"
            assert result.version_id == "version-1"
            assert author.saved_metadata == {"origin_candidate_id": candidate.id}
            again = await service.confirm(
                "u1",
                candidate.id,
                expected_revision=candidate.revision,
                package_sha256=candidate.package_sha256,
                idempotency_key="idem-1",
            )
            assert again.skill_id == result.skill_id
            assert author.create_calls == 1
        finally:
            await engine.dispose()

    run(scenario())


def test_confirm_rejects_tampered_hash_and_foreign_owner(tmp_path) -> None:
    async def scenario():
        repo, service, ops, _author, engine = await skill_stack(tmp_path)
        try:
            ops.skill_result = valid_skill_draft()
            await success_episode(repo, "e1", thread="t1")
            candidate = (
                await service.generate(
                    "u1", explicit_episode_ids=["e1"], explicit_user_request=True
                )
            )[0]
            with pytest.raises(MemoryConflictError, match="hash"):
                await service.confirm(
                    "u1",
                    candidate.id,
                    expected_revision=candidate.revision,
                    package_sha256="0" * 64,
                    idempotency_key="idem-2",
                )
            with pytest.raises(MemoryNotFoundError, match="not found"):
                await service.confirm(
                    "u2",
                    candidate.id,
                    expected_revision=candidate.revision,
                    package_sha256=candidate.package_sha256,
                    idempotency_key="idem-3",
                )
        finally:
            await engine.dispose()

    run(scenario())
