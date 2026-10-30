"""Profile HTTP surface for the additive OpenViking adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.profile import ProfileService
from agentkit.persistence.base import Base
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import openviking_profile


@pytest.fixture
def client(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text("---\npreferred_name: 小王\nlanguage: zh-CN\n---\n", encoding="utf-8")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(
                    sync_conn, tables=[Base.metadata.tables["memory_profile_documents"]]
                )
            )

    asyncio.run(setup())
    service = ProfileService(async_sessionmaker(engine, expire_on_commit=False), seed)
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(openviking_profile.router, prefix="/api")
    app.state.profile_service = service
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http
    asyncio.run(engine.dispose())


def test_get_patch_conflict_and_clear(client) -> None:
    initial = client.get("/api/memory/profile")
    assert initial.status_code == 200
    body = initial.json()
    assert body["fields"]["identity.preferred_name"]["value"] == "小王"
    assert body["fields"]["identity.preferred_name"]["source"] == "user_manual"
    assert body["schema_version"] == 1
    assert isinstance(body["revision"], int)
    etag = body["etag"]

    patched = client.patch(
        "/api/memory/profile",
        json={"fields": {"communication.tone": "direct"}, "etag": etag},
    )
    assert patched.status_code == 200
    assert patched.json()["fields"]["communication.tone"]["value"] == "direct"

    stale = client.patch(
        "/api/memory/profile",
        json={"fields": {"communication.verbosity": "concise"}, "etag": etag},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "profile_conflict"

    invalid = client.patch(
        "/api/memory/profile",
        json={"fields": {"unknown.field": "x"}, "etag": patched.json()["etag"]},
    )
    assert invalid.status_code == 422

    # The existing profile page still sends expected_revision and no body on
    # DELETE; both compatibility paths must keep working during cutover.
    legacy = client.patch(
        "/api/memory/profile",
        json={
            "fields": {"communication.verbosity": "concise"},
            "expected_revision": patched.json()["revision"],
        },
    )
    assert legacy.status_code == 200
    assert legacy.json()["fields"]["communication.verbosity"]["value"] == "concise"

    stale_revision = client.patch(
        "/api/memory/profile",
        json={
            "fields": {"communication.verbosity": "balanced"},
            "expected_revision": patched.json()["revision"],
        },
    )
    assert stale_revision.status_code == 409

    deleted = client.request(
        "DELETE",
        "/api/memory/profile/identity.preferred_name",
    )
    assert deleted.status_code == 200
    assert "identity.preferred_name" not in deleted.json()["fields"]

    cleared = client.request("DELETE", "/api/memory/profile")
    assert cleared.status_code == 200
    assert cleared.json()["fields"] == {}


def test_legacy_navigation_endpoints_are_empty_not_404(client) -> None:
    pending = client.get("/api/memory/pending")
    candidates = client.get("/api/memory/skill-candidates")

    assert pending.status_code == 200
    assert pending.json() == {"items": [], "next_cursor": None}
    assert candidates.status_code == 200
    assert candidates.json() == []
