"""Onboarding state for the DB-backed profile surface."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import ProfileService, render_profile_prompt
from agentkit.persistence.base import Base


@pytest_asyncio.fixture
async def stack(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: ''\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_profile_documents"],
        Base.metadata.tables["memory_profile_onboarding"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile = ProfileService(factory, seed)
    onboarding = OnboardingService(factory, profile)
    yield profile, onboarding
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_prompt_uses_user_profile_tag(stack) -> None:
    profile, _onboarding = stack
    snapshot = await profile.get_snapshot("u1")
    snapshot = await profile.patch(
        "u1",
        {"identity.preferred_name": "小王"},
        etag=snapshot.etag,
    )

    rendered = render_profile_prompt(snapshot)

    assert rendered.startswith('<user_profile source="memory_profile_documents"')
    assert rendered.endswith("</user_profile>")
    assert snapshot.etag in rendered
    assert "小王" in rendered


@pytest.mark.asyncio
async def test_onboarding_is_seen_then_can_be_snoozed_and_completed(stack) -> None:
    _profile, onboarding = stack

    first = await onboarding.get("u1")
    assert first.status == "seen"
    assert first.should_show is True
    assert first.profile_field_count == 0

    snoozed = await onboarding.update("u1", "snooze")
    assert snoozed.status == "snoozed"
    assert (await onboarding.get("u1")).should_show is False

    completed = await onboarding.update("u1", "complete")
    assert completed.status == "completed"
    assert (await onboarding.get("u1")).should_show is False


@pytest.mark.asyncio
async def test_onboarding_is_suppressed_during_an_active_run(stack) -> None:
    _profile, onboarding = stack

    assert (await onboarding.get("u1", active_run=True)).should_show is False
