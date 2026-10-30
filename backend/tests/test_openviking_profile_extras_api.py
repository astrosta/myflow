"""HTTP surface for onboarding and profile suggestions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import ProfileService
from agentkit.agents.memory.openviking.suggestions import (
    ProfileSuggester,
    ProfileSuggestionStore,
)
from agentkit.persistence.base import Base
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import openviking_profile


class Facade:
    def __init__(self) -> None:
        self.hits: list[MemoryHit] = []

    async def search(self, user_id, query, *, memory_types, limit=8, **kwargs):
        return self.hits


@pytest.fixture
def client(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: ''\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_profile_documents"],
        Base.metadata.tables["memory_profile_onboarding"],
        Base.metadata.tables["memory_profile_suggestions"],
    ]
    asyncio.run(
        _create(engine, tables)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile = ProfileService(factory, seed)
    onboarding = OnboardingService(factory, profile)
    facade = Facade()
    suggestions = ProfileSuggester(ProfileSuggestionStore(factory), profile, facade)
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(openviking_profile.router, prefix="/api")
    app.state.profile_service = profile
    app.state.onboarding_service = onboarding
    app.state.suggestion_service = suggestions
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http, facade
    asyncio.run(engine.dispose())


async def _create(engine, tables) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))


def test_onboarding_actions(client) -> None:
    http, _facade = client

    first = http.get("/api/memory/profile/onboarding")
    assert first.status_code == 200
    assert first.json()["should_show"] is True
    assert first.json()["status"] == "seen"

    snoozed = http.post("/api/memory/profile/onboarding", json={"action": "snooze"})
    assert snoozed.status_code == 200
    assert snoozed.json()["status"] == "snoozed"
    assert snoozed.json()["should_show"] is False


def test_saving_a_basic_profile_completes_onboarding(client) -> None:
    http, _facade = client
    profile = http.get("/api/memory/profile").json()
    http.get("/api/memory/profile/onboarding")

    saved = http.patch(
        "/api/memory/profile",
        json={
            "fields": {
                "identity.preferred_name": "小王",
                "communication.language": "zh-CN",
            },
            "etag": profile["etag"],
        },
    )
    assert saved.status_code == 200
    state = http.get("/api/memory/profile/onboarding").json()
    assert state["status"] == "completed"


def test_suggestions_list_accept_and_dismiss(client) -> None:
    http, facade = client
    facade.hits = [
        MemoryHit(
            uri="viking://user/u/memories/preferences/style.md",
            memory_type="preferences",
            abstract="希望被称呼为小王，偏好简洁",
        )
    ]

    listed = http.get("/api/memory/profile/suggestions")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert {item["field_path"] for item in items} >= {
        "identity.preferred_name",
        "communication.verbosity",
    }

    accepted = http.post(
        f"/api/memory/profile/suggestions/{items[0]['id']}/accept",
        json={},
    )
    assert accepted.status_code == 200
    assert accepted.json()["suggestion"]["status"] == "accepted"
    assert accepted.json()["profile"]["fields"]

    dismissed = http.post(
        f"/api/memory/profile/suggestions/{items[1]['id']}/dismiss",
        json={},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"
