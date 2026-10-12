"""M6 thread routes: state edit, compact, history, and branches."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.runtime.runs.manager import RunManager
from agentkit.persistence.run.memory import MemoryRunStore
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import threads


def _client() -> TestClient:
    services = SimpleNamespace(
        run_manager=RunManager(MemoryRunStore()),
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=MemoryThreadMetaStore(), checkpointer=MemorySaver()),
        run_store=MemoryRunStore(),
        event_store=SimpleNamespace(),
    )
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(threads.router, prefix="/api")
    application.state.run_services = services
    return TestClient(application, raise_server_exceptions=False)


def test_state_edit_rejects_unknown_channels_and_empty_values_read_the_head() -> None:
    client = _client()
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201

    unknown = client.post("/api/threads/thread-1/state", json={"values": {"not_a_channel": 1}})
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "invalid_state_update"

    empty = client.post("/api/threads/thread-1/state", json={})
    assert empty.status_code == 200
    assert empty.json()["checkpoint_id"] is None
    assert empty.json()["values"] == {}

    missing = client.post("/api/threads/missing/state", json={})
    assert missing.status_code == 404
    assert missing.json()["code"] == "thread_not_found"


def test_compact_reports_disabled_and_history_is_an_empty_page(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.summarization_middleware.create_summarization_middleware",
        lambda **kwargs: None,
    )
    client = _client()
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201

    compact = client.post("/api/threads/thread-1/compact", json={})
    assert compact.status_code == 409
    assert compact.json()["code"] == "compaction_disabled"
    assert "Failed to compact" not in compact.text

    history = client.get("/api/threads/thread-1/history")
    assert history.status_code == 200
    assert history.json() == {"data": [], "has_more": False, "next_cursor": None}

    branches = client.get("/api/threads/thread-1/branches")
    assert branches.status_code == 200
    assert branches.json() == {"branches": []}

    sideways = client.get("/api/threads/thread-1/history", params={"limit": 201})
    assert sideways.status_code == 422
    assert sideways.json()["code"] == "invalid_request"
