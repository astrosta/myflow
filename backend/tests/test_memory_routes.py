"""M10 section 2.12: memory HTTP surface."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from agentkit.agents.memory.backends.langmem.backend import LangMemBackend
from agentkit.agents.memory.manager import MemoryManager, reset_memory_manager
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.profile import ProfileService
from agentkit.agents.memory.services.semantic import SemanticMemoryService
from agentkit.agents.memory.types import MemoryProposal, MemoryScope
from agentkit.config.memory_config import MemoryConfig
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import memory


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_memory_manager()
    yield
    reset_memory_manager()


@pytest.fixture
def client():
    backend = LangMemBackend(InMemoryStore())
    resolver = MemoryResolver()
    profile = ProfileService(backend, resolver)
    semantic = SemanticMemoryService(backend, resolver)
    manager = MemoryManager(
        backend,
        [profile, semantic],
        config=MemoryConfig(
            enabled=True,
            maintenance={"min_search_calls_per_window": 0, "shadow": True},
        ),
    )
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(memory.router, prefix="/api")
    app.state.memory_manager = manager
    return TestClient(app, raise_server_exceptions=False), manager


def test_profile_routes_cas_and_validation(client) -> None:
    http, _manager = client
    initial = http.get("/api/memory/profile")
    assert initial.status_code == 200
    assert initial.json() == {"schema_version": 1, "revision": 0, "fields": {}}

    written = http.patch(
        "/api/memory/profile",
        json={"fields": {"communication.language": "zh-CN"}, "expected_revision": 0},
    )
    assert written.status_code == 200
    body = written.json()
    assert body["revision"] == 1
    assert body["fields"]["communication.language"]["value"] == "zh-CN"

    stale = http.patch(
        "/api/memory/profile",
        json={"fields": {"communication.tone": "direct"}, "expected_revision": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "memory_conflict"

    invalid = http.patch(
        "/api/memory/profile",
        json={"fields": {"notes": "free text"}, "expected_revision": 1},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_memory_profile"

    cleared = http.delete("/api/memory/profile/communication.language")
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": "communication.language"}
    assert http.get("/api/memory/profile").json()["fields"] == {}


def test_search_items_remember_and_export(client) -> None:
    http, manager = client
    scope = MemoryScope("default")
    record = manager.service("semantic").write(
        scope,
        [
            MemoryProposal(
                id="fact-1",
                kind="semantic",
                payload={
                    "content": "用户偏好 pytest 做 Python 测试",
                    "topic_key": "python.test",
                    "category": "preference",
                    "source": "inferred",
                },
                evidence="我们一般用 pytest",
            )
        ],
    )[0]

    search = http.get("/api/memory/search", params={"q": "pytest"})
    assert search.status_code == 200
    assert search.json()["items"][0]["record"]["id"] == record.id

    listed = http.get("/api/memory/items", params={"kind": "semantic"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == record.id

    remembered = http.post(
        f"/api/memory/items/semantic/{record.id}/remember", json={"explicit": True}
    )
    assert remembered.status_code == 200
    assert remembered.json()["explicit_remember"] is True

    facts = http.get("/api/memory/facts")
    assert facts.status_code == 200
    assert facts.json()["items"][0]["topic_key"] == "python.test"

    exported = http.get("/api/memory/export")
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == 1
    assert exported.json()["records"]

    deleted = http.delete(f"/api/memory/items/semantic/{record.id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": record.id}
    after = http.get("/api/memory/items", params={"kind": "semantic"})
    assert after.json()["items"] == []


def test_conflict_route_resolves_and_clear_removes_everything(client) -> None:
    http, manager = client
    scope = MemoryScope("default")
    for content in ("用户喜欢 pytest", "用户喜欢 unittest"):
        manager.service("semantic").write(
            scope,
            [
                MemoryProposal(
                    id=content,
                    kind="semantic",
                    payload={
                        "content": content,
                        "topic_key": "python.test",
                        "category": "preference",
                        "source": "inferred",
                    },
                    evidence="",
                )
            ],
        )
    conflicts = http.get("/api/memory/conflicts")
    assert conflicts.status_code == 200
    conflict = conflicts.json()["items"][0]

    resolved = http.post(
        f"/api/memory/conflicts/{conflict['id']}/resolve", json={"resolution": "candidate"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    http.patch(
        "/api/memory/profile",
        json={"fields": {"communication.language": "zh-CN"}, "expected_revision": 0},
    )
    cleared = http.delete("/api/memory")
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": True}
    assert http.get("/api/memory/profile").json()["fields"] == {}
    assert http.get("/api/memory/items", params={"kind": "semantic"}).json()["items"] == []


def test_unknown_kind_is_501_and_missing_item_is_404(client) -> None:
    http, _manager = client
    unknown = http.get("/api/memory/items", params={"kind": "future-kind"})
    assert unknown.status_code == 501
    assert unknown.json()["code"] == "memory_kind_unsupported"

    missing = http.get("/api/memory/items/semantic/missing")
    assert missing.status_code == 404
    assert missing.json()["code"] == "memory_not_found"


def test_maintenance_route_is_admin_and_dry_run(client) -> None:
    http, _manager = client
    response = http.post("/api/memory/maintenance/run", params={"dry_run": True})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert "run_id" in body
