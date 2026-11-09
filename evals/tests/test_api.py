"""Gateway thread, run, and event client tests.

Everything here is black-box HTTP: an ``httpx.MockTransport`` stands in for the
Gateway, so the eval package never imports backend.app or agentkit.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from myflow_evals import api
from myflow_evals.accounts import AccountCredential, AccountSession
from myflow_evals.api import AgentApi, RunSnapshot

BASE_URLS = ["http://gateway.test/api", "http://gateway.test"]
THREAD_ID = "thread-1"
RUN_ID = "run-1"
CSRF_TOKEN = "csrf-token"
ALICE = AccountCredential(label="analyst_a", username="alice", password="alice-password")


def _session(
    base_url: str, handler: Callable[[httpx.Request], httpx.Response]
) -> AccountSession:
    """A logged-in session whose Client owns the cookie jar and CSRF header."""
    client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    client.cookies.set("access_token", "access-token")
    return AccountSession(
        credential=ALICE,
        client=client,
        user_id="11111111-1111-4111-8111-111111111111",
        hashed_user_id="hashed-user-id",
    )


@pytest.mark.parametrize("base_url", BASE_URLS)
def test_create_thread_posts_thread_id_and_metadata(base_url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"thread_id": THREAD_ID, "status": "idle"})

    session = _session(base_url, handler)

    assert AgentApi.create_thread(session, THREAD_ID, {"sample_id": "sample-1"}) is None

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/threads"
    assert request.headers["x-csrf-token"] == CSRF_TOKEN
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "thread_id": THREAD_ID,
        "metadata": {"sample_id": "sample-1"},
    }


def test_create_thread_propagates_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "thread not found"})

    session = _session(BASE_URLS[0], handler)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        AgentApi.create_thread(session, THREAD_ID, None)

    assert excinfo.value.response.status_code == 404


def test_start_run_sends_human_message_and_keeps_the_connection() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            202,
            json={"run_id": RUN_ID, "thread_id": THREAD_ID, "status": "pending"},
        )

    session = _session(BASE_URLS[0], handler)

    run_id = AgentApi.start_run(
        session,
        THREAD_ID,
        "What is the refund policy?",
        {"sample_id": "sample-1"},
        {"agent_name": "lead"},
    )

    assert run_id == RUN_ID
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/threads/thread-1/runs"
    assert request.headers["x-csrf-token"] == CSRF_TOKEN
    assert json.loads(request.content) == {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"type": "human", "content": "What is the refund policy?"}]},
        "metadata": {"sample_id": "sample-1"},
        "context": {"agent_name": "lead"},
        "on_disconnect": "continue",
    }


def test_start_run_defaults_optional_metadata_and_context_to_null() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"run_id": RUN_ID, "status": "pending"})

    session = _session(BASE_URLS[0], handler)

    assert AgentApi.start_run(session, THREAD_ID, "hello") == RUN_ID

    body = json.loads(requests[0].content)
    assert body["metadata"] is None
    assert body["context"] is None


def test_start_run_rejects_a_response_without_run_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "pending"})

    session = _session(BASE_URLS[0], handler)

    with pytest.raises(RuntimeError, match="run id"):
        AgentApi.start_run(session, THREAD_ID, "hello")


def test_wait_run_polls_until_terminal_and_reads_the_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_POLL_INTERVAL_SECONDS", 0.0)
    requests: list[httpx.Request] = []
    states = [
        {"run_id": RUN_ID, "status": "pending", "metadata": {}},
        {"run_id": RUN_ID, "status": "running", "metadata": {}},
        {
            "run_id": RUN_ID,
            "status": "success",
            "error": None,
            "metadata": {"myflow-trace_id": "trace-123"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=states.pop(0))

    session = _session(BASE_URLS[0], handler)

    snapshot = AgentApi.wait_run(session, THREAD_ID, RUN_ID)

    assert snapshot == RunSnapshot(
        run_id=RUN_ID, status="success", error=None, trace_id="trace-123"
    )
    assert [request.method for request in requests] == ["GET", "GET", "GET"]
    assert [request.url.path for request in requests] == [
        "/api/threads/thread-1/runs/run-1",
        "/api/threads/thread-1/runs/run-1",
        "/api/threads/thread-1/runs/run-1",
    ]


@pytest.mark.parametrize(
    ("status", "error"),
    [("error", "agent exploded"), ("timeout", "Run execeed 30 seconds")],
)
def test_wait_run_returns_terminal_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch, status: str, error: str
) -> None:
    monkeypatch.setattr(api, "_POLL_INTERVAL_SECONDS", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "status": status, "error": error, "metadata": {}},
        )

    session = _session(BASE_URLS[0], handler)

    snapshot = AgentApi.wait_run(session, THREAD_ID, RUN_ID)

    assert snapshot.status == status
    assert snapshot.error == error
    assert snapshot.trace_id is None


def test_wait_run_treats_interrupted_as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_POLL_INTERVAL_SECONDS", 0.0)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"run_id": RUN_ID, "status": "interrupted", "metadata": {}}
        )

    session = _session(BASE_URLS[0], handler)

    snapshot = AgentApi.wait_run(session, THREAD_ID, RUN_ID)

    assert snapshot.status == "interrupted"
    assert len(requests) == 1


def test_wait_run_ignores_a_non_string_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_POLL_INTERVAL_SECONDS", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "status": "success", "metadata": {"myflow-trace_id": 7}},
        )

    session = _session(BASE_URLS[0], handler)

    assert AgentApi.wait_run(session, THREAD_ID, RUN_ID).trace_id is None


def test_wait_run_propagates_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Run run-1 not found"})

    session = _session(BASE_URLS[0], handler)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        AgentApi.wait_run(session, THREAD_ID, RUN_ID)

    assert excinfo.value.response.status_code == 404


def test_list_events_follows_pagination_and_sends_one_event_types_param() -> None:
    requests: list[httpx.Request] = []
    pages = [
        {
            "data": [{"seq": 1, "run_id": RUN_ID, "event_type": "llm.ai.response"}],
            "next_cursor": "1",
            "has_more": True,
        },
        {
            "data": [{"seq": 2, "run_id": RUN_ID, "event_type": "retrieval.kb"}],
            "next_cursor": None,
            "has_more": False,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages.pop(0))

    session = _session(BASE_URLS[0], handler)

    rows = AgentApi.list_events(
        session,
        THREAD_ID,
        RUN_ID,
        event_types=["llm.ai.response", "retrieval.kb"],
    )

    assert [row["seq"] for row in rows] == [1, 2]
    assert len(requests) == 2
    assert requests[0].url.path == "/api/threads/thread-1/runs/run-1/events"
    assert requests[0].url.params.get("event_types") == "llm.ai.response,retrieval.kb"
    assert requests[0].url.params.get("after_seq") is None
    assert requests[1].url.params.get("after_seq") == "1"


def test_list_events_starts_at_the_callers_after_seq() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [], "next_cursor": None, "has_more": False})

    session = _session(BASE_URLS[0], handler)

    assert AgentApi.list_events(session, THREAD_ID, RUN_ID, after_seq=7) == []

    assert requests[0].url.params.get("after_seq") == "7"


def test_list_events_omits_an_empty_event_types_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [], "next_cursor": None, "has_more": False})

    session = _session(BASE_URLS[0], handler)

    AgentApi.list_events(session, THREAD_ID, RUN_ID, event_types=[])

    assert requests[0].url.params.get("event_types") is None


def test_list_events_rejects_has_more_without_a_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "next_cursor": None, "has_more": True})

    session = _session(BASE_URLS[0], handler)

    with pytest.raises(RuntimeError, match="next_cursor"):
        AgentApi.list_events(session, THREAD_ID, RUN_ID)


def test_list_events_propagates_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Run run-1 not found"})

    session = _session(BASE_URLS[0], handler)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        AgentApi.list_events(session, THREAD_ID, RUN_ID)

    assert excinfo.value.response.status_code == 404
