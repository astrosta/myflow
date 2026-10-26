"""Task 6: L1 gate, episode dehydration, idempotency, and correction."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.episodic import EpisodicService, GateSignals, L1Gate
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.proposals import DehydratedEpisode, EpisodeProposal
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.sessions import SessionService
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def make_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "e.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    config = MemoryConfig()
    return repo, SessionService(repo, config), FakeOperators()


def test_l1_gate_uses_signals_and_threshold() -> None:
    gate = L1Gate(threshold=0.55)
    assert gate.evaluate(GateSignals(tool_error=True, retry=True)).passed is True
    assert gate.evaluate(GateSignals(greeting=True)).passed is False


def test_episode_job_is_idempotent_and_has_no_transcript(tmp_path) -> None:
    async def scenario():
        repo, sessions, ops = await make_stack(tmp_path)
        ops.episode_result = EpisodeProposal(
            episode=DehydratedEpisode(
                task_intent="修复部署失败",
                final_outcome="SUCCESS",
                key_takeaway="重启 upstream",
                evidence_refs=["message:a1"],
                task_class="deploy_fix",
                key_steps=["exec", "read"],
            )
        )
        captured = await sessions.capture_turn(
            "u1",
            "t1",
            "r1",
            [HumanMessage(id="u1", content="部署失败后修复"), AIMessage(id="a1", content="已修复")],
            terminal=True,
        )
        service = EpisodicService(repo, ops, L1Gate(0.55), max_summary_tokens=1200)
        first = await service.process_ingest_job(
            captured.job_id, signals=GateSignals(tool_error=True, retry=True)
        )
        second = await service.process_ingest_job(
            captured.job_id, signals=GateSignals(tool_error=True, retry=True)
        )
        assert first is not None and second is not None
        assert first.id == second.id
        assert first.payload["final_outcome"] == "SUCCESS"
        assert "messages" not in first.payload
        assert "已修复" not in str(first.payload)
        assert first.consolidation_status == "pending"

    run(scenario())


def test_correction_supersedes_old_episode(tmp_path) -> None:
    async def scenario():
        repo, sessions, ops = await make_stack(tmp_path)
        ops.episode_result = EpisodeProposal(
            episode=DehydratedEpisode(
                task_intent="旧任务",
                final_outcome="PARTIAL",
                evidence_refs=["message:a1"],
            )
        )
        captured = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="任务"), AIMessage(id="a1", content="完成")],
            terminal=True,
        )
        service = EpisodicService(repo, ops, L1Gate(0.0), max_summary_tokens=1200)
        old = await service.process_ingest_job(captured.job_id)
        assert old is not None
        corrected = await service.correct(
            old.id,
            content="更正后的摘要",
            outcome="SUCCESS",
            evidence_refs=["message:new"],
            expected_revision=old.revision,
        )
        assert corrected.correction_of == old.id
        current = await repo.get_record("u1", old.id)
        assert current is not None and current.status == "corrected"

    run(scenario())
