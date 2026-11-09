"""Read-only run-event API: auth, ownership, filters, and bounded pages."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.persistence.events.memory import MemoryRunEventStore
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.runtime.runs import DisconnectMode, RunRecord, RunStatus
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.deps import RunServices
from backend.app.gateway.routers import thread_runs
from backend.app.gateway.run_models import CallerKind, RunCaller

THREAD_ID = "thread-1"
RUN_ID = "run-1"
ALICE = "alice"
BOB = "bob"


def _event(event_type: str, category: str, content: object, **extra: object) -> dict:
    event = {
        "thread_id": THREAD_ID,
        "run_id": RUN_ID,
        "event_type": event_type,
        "category": category,
        "content": content,
        "metadata": {},
    }
    event.update(extra)
    return event


def _services(*, run_owner: str = ALICE) -> SimpleNamespace:
    thread_store = MemoryThreadMetaStore()
    asyncio.run(thread_store.create(THREAD_ID, user_id=ALICE))
    event_store = MemoryRunEventStore()
    asyncio.run(
        event_store.put_batch(
            [
                _event("run.start", "trace", {"step": "start"}, user_id=ALICE),
                _event(
                    "subagent.start",
                    "subagent",
                    {"agent": "researcher"},
                    metadata={"task_id": "call_a"},
                    user_id=ALICE,
                ),
                _event(
                    "subagent.end",
                    "subagent",
                    {"summary": "done"},
                    metadata={"task_id": "call_a"},
                    user_id=ALICE,
                ),
            ]
        )
    )
    record = RunRecord(
        run_id=RUN_ID,
        thread_id=THREAD_ID,
        assistant_id="lead_agent",
        status=RunStatus.success,
        on_disconnect=DisconnectMode.cancel,
        user_id=run_owner,
    )

    async def get(run_id: str, *, user_id: str | None) -> RunRecord | None:
        if run_id != RUN_ID or (user_id is not None and record.user_id != user_id):
            return None
        return record

    return SimpleNamespace(
        run_manager=SimpleNamespace(get=get),
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=thread_store),
        run_store=SimpleNamespace(),
        event_store=event_store,
    )


def _client(
    services: RunServices,
    monkeypatch,
    *,
    user_id: str = ALICE,
    scopes: frozenset[str] | None = None,
) -> TestClient:
    caller = RunCaller(kind=CallerKind.HTTP, user_id=user_id, scopes=scopes)
    monkeypatch.setattr("backend.app.gateway.deps.caller_from_request", lambda request: caller)
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(thread_runs.router, prefix="/api")
    application.state.run_services = services
    return TestClient(application, raise_server_exceptions=False)


def _events_url() -> str:
    return f"/api/threads/{THREAD_ID}/runs/{RUN_ID}/events"


def test_run_events_require_runs_read(monkeypatch) -> None:
    client = _client(_services(), monkeypatch, scopes=frozenset({"threads:read"}))

    response = client.get(_events_url())

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_another_owner_gets_404_for_a_hidden_thread(monkeypatch) -> None:
    client = _client(_services(), monkeypatch, user_id=BOB)

    response = client.get(_events_url())

    assert response.status_code == 404
    assert response.json()["code"] == "thread_not_found"


def test_another_owner_gets_404_for_a_run(monkeypatch) -> None:
    client = _client(_services(run_owner=BOB), monkeypatch, user_id=ALICE)

    response = client.get(_events_url())

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_event_types_filter_and_public_fields(monkeypatch) -> None:
    client = _client(_services(), monkeypatch)

    response = client.get(_events_url(), params={"event_types": "subagent.start"})

    assert response.status_code == 200
    body = response.json()
    assert [row["event_type"] for row in body["data"]] == ["subagent.start"]
    row = body["data"][0]
    assert row["content"] == {"agent": "researcher"}
    assert row["metadata"] == {"task_id": "call_a"}
    assert row["category"] == "subagent"
    assert "user_id" not in json.dumps(body)


def test_task_id_filter_selects_one_delegation(monkeypatch) -> None:
    client = _client(_services(), monkeypatch)

    response = client.get(_events_url(), params={"task_id": "call_a"})

    assert response.status_code == 200
    assert [row["event_type"] for row in response.json()["data"]] == [
        "subagent.start",
        "subagent.end",
    ]


def test_limit_sets_has_more_and_the_seq_cursor(monkeypatch) -> None:
    client = _client(_services(), monkeypatch)

    first = client.get(_events_url(), params={"limit": 1})
    assert first.status_code == 200
    assert first.json()["has_more"] is True
    assert first.json()["next_cursor"] == "1"
    assert [row["seq"] for row in first.json()["data"]] == [1]

    second = client.get(_events_url(), params={"limit": 1, "after_seq": 1})
    assert second.status_code == 200
    assert second.json()["has_more"] is True
    assert second.json()["next_cursor"] == "2"
    assert [row["seq"] for row in second.json()["data"]] == [2]

    last = client.get(_events_url(), params={"limit": 1, "after_seq": 2})
    assert last.status_code == 200
    assert last.json()["has_more"] is False
    assert last.json()["next_cursor"] is None
    assert [row["seq"] for row in last.json()["data"]] == [3]
