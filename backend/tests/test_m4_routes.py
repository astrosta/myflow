"""M4 HTTP surface: thread lists, search, patch, delete, messages, and run pages."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.events.memory import MemoryRunEventStore
from agentkit.runtime.runs.schemas import ThreadOperationKind
from agentkit.persistence.run.memory import MemoryRunStore
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import thread_runs, threads

_EMPTY_PAGE = {"data": [], "has_more": False, "next_cursor": None}


def _services() -> SimpleNamespace:
    thread_store = MemoryThreadMetaStore()
    run_store = MemoryRunStore()
    event_store = MemoryRunEventStore()
    return SimpleNamespace(
        run_manager=SimpleNamespace(),
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=thread_store, checkpointer=SimpleNamespace()),
        run_store=run_store,
        event_store=event_store,
        threads=thread_store,
        runs=run_store,
        events=event_store,
    )


def _client(services: SimpleNamespace) -> TestClient:
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(threads.router, prefix="/api")
    application.include_router(thread_runs.router, prefix="/api")
    application.state.run_services = services
    return TestClient(application, raise_server_exceptions=False)


def test_empty_thread_list_is_an_empty_page() -> None:
    response = _client(_services()).get("/api/threads")

    assert response.status_code == 200
    assert response.json() == _EMPTY_PAGE


def test_limit_above_the_maximum_is_422() -> None:
    response = _client(_services()).get("/api/threads", params={"limit": 201})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_unknown_order_is_422() -> None:
    response = _client(_services()).get("/api/threads", params={"order": "sideways"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_thread_list_pages_newest_first() -> None:
    services = _services()
    client = _client(services)
    for thread_id in ("thread-a", "thread-b", "thread-c"):
        assert client.post("/api/threads", json={"thread_id": thread_id}).status_code == 201

    first = client.get("/api/threads", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert [row["thread_id"] for row in body["data"]] == ["thread-c", "thread-b"]
    assert body["has_more"] is True
    assert body["next_cursor"]

    second = client.get("/api/threads", params={"limit": 2, "cursor": body["next_cursor"]})
    assert [row["thread_id"] for row in second.json()["data"]] == ["thread-a"]
    assert second.json()["has_more"] is False
    assert second.json()["next_cursor"] is None


def test_search_filters_metadata_and_rejects_unsafe_keys() -> None:
    services = _services()
    client = _client(services)
    assert client.post("/api/threads", json={"thread_id": "keep", "metadata": {"topic": "m4"}}).status_code == 201
    assert client.post("/api/threads", json={"thread_id": "drop", "metadata": {"topic": "other"}}).status_code == 201

    found = client.post("/api/threads/search", json={"metadata": {"topic": "m4"}})
    assert found.status_code == 200
    assert [row["thread_id"] for row in found.json()["data"]] == ["keep"]

    rejected = client.post("/api/threads/search", json={"metadata": {"bad key": "x"}})
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_metadata_filter"


def test_get_patch_and_delete_thread() -> None:
    services = _services()
    client = _client(services)
    assert client.post("/api/threads", json={"thread_id": "thread-1", "metadata": {"topic": "m4"}}).status_code == 201

    missing = client.get("/api/threads/missing")
    assert missing.status_code == 404
    assert missing.json()["code"] == "thread_not_found"

    found = client.get("/api/threads/thread-1")
    assert found.status_code == 200
    assert found.json()["metadata"] == {"topic": "m4"}

    patched = client.patch("/api/threads/thread-1", json={"metadata": {"pinned": True}})
    assert patched.status_code == 200
    assert patched.json()["metadata"] == {"topic": "m4", "pinned": True}

    removed = client.delete("/api/threads/thread-1")
    assert removed.status_code == 200
    assert removed.json() == {"deleted": "thread-1"}
    assert client.get("/api/threads/thread-1").status_code == 404


def test_delete_refuses_a_running_thread() -> None:
    services = _services()
    client = _client(services)
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201
    services.runs._rows["run-live"] = {
        "run_id": "run-live",
        "thread_id": "thread-1",
        "assistant_id": "lead_agent",
        "status": "running",
        "operation_kind": ThreadOperationKind.run,
        "created_at": "2026-10-04T00:00:00Z",
        "updated_at": "2026-10-04T00:00:00Z",
        "metadata": {},
        "kwargs": {},
        "on_disconnect": "cancel",
        "multitask_strategy": "reject",
        "user_id": "default",
        "error": None,
    }

    response = client.delete("/api/threads/thread-1")

    assert response.status_code == 409
    assert response.json()["code"] == "thread_busy"


def test_messages_page_is_ascending_and_older_pages_use_the_cursor() -> None:
    services = _services()
    client = _client(services)
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201
    import asyncio

    asyncio.run(
        services.events.put_batch(
            [
                {
                    "thread_id": "thread-1",
                    "run_id": "run-1",
                    "user_id": "default",
                    "event_type": "human",
                    "category": "message",
                    "content": text,
                    "metadata": {},
                }
                for text in ("one", "two", "three")
            ]
        )
    )

    assert client.get("/api/threads/missing/messages").status_code == 404
    first = client.get("/api/threads/thread-1/messages", params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert [row["content"] for row in body["data"]] == ["two", "three"]
    assert [row["seq"] for row in body["data"]] == [2, 3]
    assert body["has_more"] is True

    older = client.get(
        "/api/threads/thread-1/messages",
        params={"limit": 2, "before_seq": body["next_cursor"]},
    )
    assert body["next_cursor"] == "3"
    assert [row["content"] for row in older.json()["data"]] == ["one", "two"]
    assert older.json()["has_more"] is False


def test_run_list_pages_and_rejects_unknown_status() -> None:
    services = _services()
    client = _client(services)
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201
    for run_id, created_at, status in (
        ("run-a", "2026-10-01T00:00:00Z", "success"),
        ("run-b", "2026-10-02T00:00:00Z", "error"),
        ("run-c", "2026-10-03T00:00:00Z", "success"),
    ):
        services.runs._rows[run_id] = {
            "run_id": run_id,
            "thread_id": "thread-1",
            "assistant_id": "lead_agent",
            "status": status,
            "operation_kind": ThreadOperationKind.run,
            "created_at": created_at,
            "updated_at": created_at,
            "metadata": {},
            "kwargs": {},
            "on_disconnect": "cancel",
            "multitask_strategy": "reject",
            "user_id": "default",
            "error": None,
        }

    assert client.get("/api/threads/missing/runs").status_code == 404
    unknown = client.get("/api/threads/thread-1/runs", params={"status": "nope"})
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "invalid_run_request"

    first = client.get("/api/threads/thread-1/runs", params={"limit": 2, "status": "success"})
    assert first.status_code == 200
    body = first.json()
    assert [row["run_id"] for row in body["data"]] == ["run-c", "run-a"]
    assert body["has_more"] is False
