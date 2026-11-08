"""HTTP surface for onboarding on the DB-backed profile."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import ProfileService
from agentkit.persistence.base import Base
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import openviking_profile


@pytest.fixture
def client(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: ''\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Base.metadata.tables["memory_profile_documents"],
        Base.metadata.tables["memory_profile_onboarding"],
    ]
    asyncio.run(_create(engine, tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile = ProfileService(factory, seed)
    onboarding = OnboardingService(factory, profile)
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(openviking_profile.router, prefix="/api")
    app.state.profile_service = profile
    app.state.onboarding_service = onboarding
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http
    asyncio.run(engine.dispose())


async def _create(engine, tables) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))


def test_onboarding_actions(client) -> None:
    http = client

    first = http.get("/api/memory/profile/onboarding")
    assert first.status_code == 200
    assert first.json()["should_show"] is True
    assert first.json()["status"] == "seen"

    snoozed = http.post("/api/memory/profile/onboarding", json={"action": "snooze"})
    assert snoozed.status_code == 200
    assert snoozed.json()["status"] == "snoozed"
    assert snoozed.json()["should_show"] is False


def test_saving_a_basic_profile_completes_onboarding(client) -> None:
    http = client
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
