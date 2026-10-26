"""Task 12: canonical memory API, profile CAS, and owner-scoped pending."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.service import build_memory_runtime
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import memory_v2


def test_profile_api_cas_and_no_pin(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "api.db").as_posix())

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return build_memory_runtime(
            MemoryConfig(enabled=True, embedding={"dimensions": 8}),
            async_sessionmaker(engine, expire_on_commit=False),
        )

    runtime = asyncio.run(setup())
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(memory_v2.router, prefix="/api")
    app.state.memory_service = runtime.service
    client = TestClient(app, raise_server_exceptions=False)
    try:
        initial = client.get("/api/memory/profile")
        assert initial.status_code == 200
        assert initial.json()["fields"] == {}

        written = client.patch(
            "/api/memory/profile",
            json={"fields": {"identity.preferred_name": "小王"}, "expected_revision": 0},
        )
        assert written.status_code == 200
        body = written.json()
        assert body["revision"] == 1
        assert body["fields"]["identity.preferred_name"]["value"] == "小王"
        assert "pinned" not in body["fields"]["identity.preferred_name"]

        stale = client.patch(
            "/api/memory/profile",
            json={"fields": {"identity.preferred_name": "小李"}, "expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "memory_conflict"

        invalid = client.patch(
            "/api/memory/profile",
            json={"fields": {"notes": "free"}, "expected_revision": 1},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_memory"

        pending = client.get("/api/memory/pending")
        assert pending.status_code == 200
        assert pending.json() == {"items": [], "next_cursor": None}
    finally:
        client.close()
        asyncio.run(runtime.service.close())
        asyncio.run(engine.dispose())
