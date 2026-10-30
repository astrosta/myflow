"""Admin status/flush API for the OpenViking memory runtime."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.agents.memory.openviking.contracts import MemoryStatus
from agentkit.agents.memory.openviking.runtime import (
    configure_openviking_memory,
    get_openviking_memory_runtime,
)
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import openviking_admin


class Facade:
    def __init__(self) -> None:
        self.flushed: list[str | None] = []

    async def status(self) -> MemoryStatus:
        return MemoryStatus(
            healthy=True,
            degraded=False,
            pending_stream_messages=2,
            pending_missing_turn_ends=1,
        )

    async def open_session_count(self, *, user_id: str | None = None) -> int:
        return 3

    async def flush(self, *, user_id: str | None = None) -> int:
        self.flushed.append(user_id)
        return 4


def client() -> tuple[TestClient, Facade]:
    facade = Facade()
    configure_openviking_memory(SimpleNamespace(facade=facade))
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(openviking_admin.router, prefix="/api")
    return TestClient(app), facade


def test_status_and_flush() -> None:
    http, facade = client()
    try:
        status = http.get("/api/admin/memory/status")
        assert status.status_code == 200
        assert status.json()["pending_stream_messages"] == 2
        assert status.json()["open_sessions"] == 3

        flushed = http.post("/api/admin/memory/flush", json={"user_id": "u1"})
        assert flushed.status_code == 200
        assert flushed.json() == {"flushed": 4}
        assert facade.flushed == ["u1"]
    finally:
        configure_openviking_memory(None)


def test_missing_runtime_is_503() -> None:
    configure_openviking_memory(None)
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(openviking_admin.router, prefix="/api")

    response = TestClient(app).get("/api/admin/memory/status")

    assert response.status_code == 503
    assert response.json()["code"] == "memory_runtime_unavailable"


def test_configured_runtime_is_cleared_after_test() -> None:
    assert get_openviking_memory_runtime() is None
