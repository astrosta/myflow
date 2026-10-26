"""Task 14: canonical service composition and profile rendering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import ProfileFieldView, ProfileSnapshotView
from agentkit.agents.memory.canonical.service import (
    build_memory_runtime,
    render_profile_view,
)
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
