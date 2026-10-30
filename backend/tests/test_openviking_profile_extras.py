"""Onboarding state and profile suggestions for the OpenViking authority."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import ProfileService, render_profile_prompt
from agentkit.agents.memory.openviking.suggestions import (
    ProfileSuggester,
    ProfileSuggestionStore,
    SuggestionConflict,
)
from agentkit.persistence.base import Base


class FakeFacade:
    def __init__(self) -> None:
        self.hits: list[MemoryHit] = []
        self.calls: list[tuple] = []

    async def search(self, user_id, query, *, memory_types, limit=8, **kwargs):
        self.calls.append((user_id, memory_types))
        return self.hits


@pytest_asyncio.fixture
async def stack(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: ''\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_profile_documents"],
        Base.metadata.tables["memory_profile_onboarding"],
        Base.metadata.tables["memory_profile_suggestions"],
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile = ProfileService(factory, seed)
    onboarding = OnboardingService(factory, profile)
    facade = FakeFacade()
    store = ProfileSuggestionStore(factory)
    suggestions = ProfileSuggester(store, profile, facade)
    yield profile, onboarding, suggestions, facade
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_prompt_uses_user_profile_tag(stack) -> None:
    profile, _onboarding, _suggestions, _facade = stack
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
    _profile, onboarding, _suggestions, _facade = stack

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
    _profile, onboarding, _suggestions, _facade = stack

    assert (await onboarding.get("u1", active_run=True)).should_show is False


@pytest.mark.asyncio
async def test_suggestions_are_generated_and_accept_updates_profile(stack) -> None:
    profile, _onboarding, suggestions, facade = stack
    facade.hits = [
        MemoryHit(
            uri="viking://user/u/memories/preferences/style.md",
            memory_type="preferences",
            abstract="用户希望被称呼为小王，偏好简洁，使用 Python，住在杭州",
        )
    ]

    items = await suggestions.get("u1")

    fields = {item.field_path for item in items}
    assert {"identity.preferred_name", "communication.verbosity", "work.tools"} <= fields
    assert all(item.profile_etag for item in items)

    accepted, view = await suggestions.accept(
        "u1",
        next(item.id for item in items if item.field_path == "communication.verbosity"),
    )
    assert accepted.status == "accepted"
    assert view.fields["communication.verbosity"] == "concise"
    assert (await profile.get_snapshot("u1")).fields["communication.verbosity"] == "concise"


@pytest.mark.asyncio
async def test_suggestion_accept_rejects_when_profile_moved(stack) -> None:
    _profile, _onboarding, suggestions, facade = stack
    facade.hits = [
        MemoryHit(
            uri="viking://user/u/memories/preferences/style.md",
            memory_type="preferences",
            abstract="偏好简洁",
        )
    ]
    items = await suggestions.get("u1")

    with pytest.raises(SuggestionConflict):
        await suggestions.accept("u1", items[0].id, etag="stale")


@pytest.mark.asyncio
async def test_dismissed_suggestion_is_not_shown_again(stack) -> None:
    _profile, _onboarding, suggestions, facade = stack
    facade.hits = [
        MemoryHit(
            uri="viking://user/u/memories/preferences/style.md",
            memory_type="preferences",
            abstract="偏好简洁",
        )
    ]
    items = await suggestions.get("u1")
    await suggestions.dismiss("u1", items[0].id)

    assert await suggestions.get("u1") == []
    assert facade.calls


@pytest.mark.asyncio
async def test_suggestion_generation_does_not_guess_ambiguous_text(stack) -> None:
    _profile, _onboarding, suggestions, _facade = stack
    assert await suggestions.get("u1") == []
