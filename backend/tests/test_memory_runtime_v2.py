"""Task 14: canonical service composition and profile rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import ProfileFieldView, ProfileSnapshotView
from agentkit.agents.memory.canonical.maintenance import MemoryMaintenanceWorker
from agentkit.agents.memory.canonical.episodic import EpisodicService, L1Gate
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.proposals import (
    DehydratedEpisode,
    EpisodeProposal,
    SemanticDocument,
    SemanticExtraction,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.semantic import SemanticService
from agentkit.agents.memory.canonical.service import (
    MemoryService,
    build_memory_runtime,
    render_profile_view,
)
from agentkit.agents.memory.canonical.sessions import SessionService
from agentkit.agents.middlewares.memory_middleware import MemoryMiddleware
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


def test_runtime_disabled_does_not_build_workers() -> None:
    runtime = build_memory_runtime(MemoryConfig(enabled=False), session_factory=None)
    assert runtime.service is None
    assert runtime.projector is None


def test_runtime_builds_service_and_reads_profile(tmp_path) -> None:
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "r.db").as_posix())
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            runtime = build_memory_runtime(
                MemoryConfig(enabled=True, embedding={"dimensions": 8}),
                async_sessionmaker(engine, expire_on_commit=False),
            )
            assert runtime.service is not None
            snapshot = await runtime.service.get_profile("u1")
            assert snapshot.revision == 0
            await runtime.service.close()
        finally:
            await engine.dispose()

    run(scenario())


def test_explicit_remember_extracts_episode_and_semantic_inline(tmp_path) -> None:
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "x.db").as_posix())
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
            config = MemoryConfig(enabled=True)
            ops = FakeOperators()
            ops.episode_result = EpisodeProposal(
                episode=DehydratedEpisode(
                    task_intent="修复部署失败",
                    final_outcome="SUCCESS",
                    key_takeaway="重启 upstream",
                    evidence_refs=["message:u1"],
                )
            )
            ops.semantic_results = [
                SemanticExtraction(
                    id="sem-1",
                    document=SemanticDocument(
                        content="用户完成了修复部署",
                        topic_key="deploy",
                        evidence_quote="修复部署",
                        evidence_tier="EXPLICIT_STATEMENT",
                    ),
                )
            ]
            sessions = SessionService(repo, config)
            episodic = EpisodicService(repo, ops, L1Gate(0.55), 1200)
            semantic = SemanticService(repo, ops, config)
            service = MemoryService(
                repo, sessions, None, semantic, episodic, None, None, None, config
            )
            result = await service.capture_turn(
                "u1",
                "t1",
                "r1",
                [HumanMessage(id="u1", content="记住这次修复部署"), AIMessage(id="a1", content="好了")],
                terminal=False,
                explicit_remember=True,
            )
            assert result.episode_id is not None
            assert result.semantic_extracted is True
            episode = await repo.get_record("u1", result.episode_id)
            assert episode is not None and episode.kind == "episodic"
            facts = await repo.list_records("u1", kind="semantic", statuses=["active"])
            assert facts and facts[0].content == "用户完成了修复部署"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_middleware_passes_explicit_remember_to_capture() -> None:
    class Service:
        canonical = True
        config = MemoryConfig(enabled=True)

        def __init__(self) -> None:
            self.calls = []

        async def capture_turn(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(session_id="s1", appended=1, closed=True, job_id="j1")

        async def update_profile_from_message(self, *args, **kwargs):
            del args, kwargs

    async def scenario():
        service = Service()
        middleware = MemoryMiddleware(service=service, user_id="u1")
        state = {
            "messages": [
                HumanMessage(id="u1", content="把刚才的修复步骤记下来"),
                AIMessage(id="a1", content="好的"),
            ]
        }
        runtime = SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"})
        await middleware.aafter_agent(state, runtime)
        await asyncio.sleep(0)
        assert service.calls
        assert service.calls[0][1]["explicit_remember"] is True

    asyncio.run(scenario())


def test_maintenance_closes_idle_and_expires_pending() -> None:
    class Sessions:
        async def close_idle_sessions(self, now):
            del now
            return 1

    class Semantic:
        async def expire_pending(self, *, now):
            del now
            return 2

    class Repo:
        async def claim_ingest_job(self, **kwargs):
            del kwargs
            return None

    service = __import__("types").SimpleNamespace(
        sessions=Sessions(), semantic=Semantic(), repo=Repo(), episodic=None
    )
    worker = MemoryMaintenanceWorker(service, MemoryConfig(enabled=True))
    result = asyncio.run(worker.run_once(now=datetime(2026, 10, 26, tzinfo=UTC)))
    assert result == {"closed": 1, "expired": 2, "episodes": 0}


def test_render_profile_view_uses_whole_fields_and_budget() -> None:
    now = datetime(2026, 10, 26, tzinfo=UTC)
    snapshot = ProfileSnapshotView(
        revision=1,
        fields={
            "identity.preferred_name": ProfileFieldView(
                value="小王",
                source="user_manual",
                evidence_tier="EXPLICIT_MANDATE",
                confidence=0.95,
                observed_at=now,
            ),
            "communication.language": ProfileFieldView(
                value="zh-CN",
                source="conversation_extracted",
                evidence_tier="EXPLICIT_STATEMENT",
                confidence=0.85,
                observed_at=now,
            ),
            "communication.tone": ProfileFieldView(
                value="direct",
                source="user_manual",
                evidence_tier="EXPLICIT_MANDATE",
                confidence=0.95,
                observed_at=now,
            ),
        },
        updated_at=now,
    )
    rendered = render_profile_view(snapshot, max_tokens=800)
    assert "<user_profile>" in rendered.text
    assert "称呼: 小王" in rendered.text
    assert "语言: zh-CN" in rendered.text
    assert "nopin" not in rendered.text
