"""Public liveness and readiness probes for the gateway."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.gateway.app import create_app, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import health as health_module

SECRET = "0123456789abcdef0123456789abcdef"


class _Session:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> None:
        if self.error is not None:
            raise self.error


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ready: bool,
    factory: Any,
) -> TestClient:
    monkeypatch.setattr(health_module, "get_session_factory", lambda: factory)
    application = FastAPI(redirect_slashes=False)
    install_middleware(
        application,
        auth_settings=AuthSettings(enabled=True, jwt_secret=SECRET),
    )
    application.include_router(health_module.router)
    application.state.ready = ready
    return TestClient(application, raise_server_exceptions=False)


def test_liveness_is_public_and_does_not_need_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, ready=False, factory=None)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_is_503_until_lifespan_marks_the_app_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _SessionFactory(_Session(error=AssertionError("database must not be touched")))
    client = _client(monkeypatch, ready=False, factory=factory)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_is_ok_without_a_persistent_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, ready=True, factory=None)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_checks_the_database_when_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        monkeypatch,
        ready=True,
        factory=_SessionFactory(_Session()),
    )

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_failure_does_not_leak_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        monkeypatch,
        ready=True,
        factory=_SessionFactory(_Session(error=RuntimeError("password hunter2"))),
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "hunter2" not in response.text


def test_the_real_application_registers_both_probes() -> None:
    paths = set(create_app().openapi()["paths"])

    assert "/health" in paths
    assert "/health/ready" in paths
