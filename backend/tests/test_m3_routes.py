"""M3 HTTP surface: route inventory, error body, and the six run/thread handlers."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.runtime.runs import CancelOutcome, DisconnectMode, RunRecord, RunStatus
from backend.app.gateway.app import app as gateway
from backend.app.gateway.app import create_app, install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import thread_runs, threads
from backend.app.gateway.run_models import RunCaller, RunLaunch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROUTER_DIR = BACKEND_ROOT / "app" / "gateway" / "routers"

# M3 and M4 rows from the router-layer table, plus the compact route already on this gateway.
EXPECTED_ROUTES = {
    ("POST", "/api/threads"),
    ("GET", "/api/threads"),
    ("POST", "/api/threads/search"),
    ("GET", "/api/threads/{thread_id}"),
    ("PATCH", "/api/threads/{thread_id}"),
    ("DELETE", "/api/threads/{thread_id}"),
    ("GET", "/api/threads/{thread_id}/state"),
    ("GET", "/api/threads/{thread_id}/messages"),
    ("POST", "/api/threads/{thread_id}/compact"),
    ("POST", "/api/threads/{thread_id}/state"),
    ("GET", "/api/threads/{thread_id}/history"),
    ("GET", "/api/threads/{thread_id}/branches"),
    ("POST", "/api/threads/{thread_id}/runs"),
    ("POST", "/api/threads/{thread_id}/runs/stream"),
    ("POST", "/api/threads/{thread_id}/runs/wait"),
    ("GET", "/api/threads/{thread_id}/runs"),
    ("GET", "/api/threads/{thread_id}/runs/{run_id}"),
    ("GET", "/api/threads/{thread_id}/runs/{run_id}/join"),
    ("POST", "/api/threads/{thread_id}/runs/{run_id}/cancel"),
    ("GET", "/api/models"),
    ("GET", "/api/models/{model_name}"),
    ("GET", "/api/threads/{thread_id}/uploads/list"),
    ("POST", "/api/threads/{thread_id}/uploads"),
    ("DELETE", "/api/threads/{thread_id}/uploads/{filename}"),
    ("GET", "/api/threads/{thread_id}/artifacts/{path:path}"),
    # M7 §2.4：MCP 配置与技能目录（没有 PUT /config：整份替换属"创建"，M7 不注册）
    ("GET", "/api/mcp/config"),
    ("PATCH", "/api/mcp/config"),
    ("POST", "/api/mcp/cache/reset"),
    ("GET", "/api/skills"),
    ("POST", "/api/skills/reload"),
    # M11 task 15: the path parameter is the internal skill_id, not the display
    # name, so every skills/{...} route in the table uses the same placeholder.
    ("PUT", "/api/skills/{skill_id}/preference"),
    ("PATCH", "/api/skills/{skill_id}"),
    # M11 task 8: Git-backed private draft author surface. /skills/mine is
    # registered before the parameterized {skill_id} routes.
    ("GET", "/api/skills/mine"),
    ("POST", "/api/skills"),
    ("POST", "/api/skills/{skill_id}/draft/open"),
    ("GET", "/api/skills/{skill_id}/draft"),
    ("GET", "/api/skills/{skill_id}/draft/tree"),
    ("GET", "/api/skills/{skill_id}/draft/file"),
    ("PUT", "/api/skills/{skill_id}/draft/file"),
    ("POST", "/api/skills/{skill_id}/draft/files"),
    ("DELETE", "/api/skills/{skill_id}/draft/file"),
    ("POST", "/api/skills/{skill_id}/draft/save"),
    ("POST", "/api/skills/{skill_id}/submit"),
    ("POST", "/api/skills/{skill_id}/withdraw"),
    ("GET", "/api/skills/{skill_id}/versions"),
    ("DELETE", "/api/skills/{skill_id}"),
    # M11 task 9: admin review and public-release surface.
    ("GET", "/api/admin/skills/reviews"),
    ("GET", "/api/admin/skills/reviews/{submission_id}"),
    ("POST", "/api/admin/skills/reviews/{submission_id}/approve"),
    ("POST", "/api/admin/skills/reviews/{submission_id}/reject"),
    ("POST", "/api/admin/skills/{skill_id}/unpublish"),
    ("POST", "/api/admin/skills/{skill_id}/activate"),
    # M11.1 task 23: 换嵌入文本/模型后的一次性强制重建（设计 §7.6）。
    ("POST", "/api/admin/skills/reindex"),
    # M10 §2.12：canonical memory（prefix=/memory，/api 由 create_app 加）
    ("GET", "/api/memory/profile"),
    ("PATCH", "/api/memory/profile"),
    ("DELETE", "/api/memory/profile/{field_path:path}"),
    ("GET", "/api/memory/search"),
    ("GET", "/api/memory/items"),
    ("GET", "/api/memory/items/{kind}/{record_id}"),
    ("DELETE", "/api/memory/items/{kind}/{record_id}"),
    ("POST", "/api/memory/items/{kind}/{record_id}/remember"),
    ("GET", "/api/memory/pending"),
    ("POST", "/api/memory/items/semantic/{record_id}/promote"),
    ("GET", "/api/memory/episodes"),
    ("POST", "/api/memory/episodes/{episode_id}/correct"),
    ("GET", "/api/memory/skill-candidates"),
    ("POST", "/api/memory/skill-candidates/{candidate_id}/confirm"),
    ("POST", "/api/memory/skill-candidates/{candidate_id}/reject"),
    ("POST", "/api/memory/reindex"),
    ("GET", "/api/memory/conflicts"),
    ("POST", "/api/memory/conflicts/{conflict_id}/resolve"),
    ("GET", "/api/memory/export"),
    ("DELETE", "/api/memory"),
    ("POST", "/api/memory/maintenance/run"),
    # Knowledge base: per-user CRUD + nested documents + retrieval test.
    ("GET", "/api/knowledge/status"),
    ("GET", "/api/knowledge/bases"),
    ("POST", "/api/knowledge/bases"),
    ("GET", "/api/knowledge/bases/{base_id}"),
    ("PATCH", "/api/knowledge/bases/{base_id}"),
    ("DELETE", "/api/knowledge/bases/{base_id}"),
    ("GET", "/api/knowledge/bases/{base_id}/documents"),
    ("POST", "/api/knowledge/bases/{base_id}/documents"),
    ("GET", "/api/knowledge/bases/{base_id}/documents/{document_id}"),
    ("POST", "/api/knowledge/bases/{base_id}/documents/{document_id}/reindex"),
    ("DELETE", "/api/knowledge/bases/{base_id}/documents/{document_id}"),
    ("GET", "/api/knowledge/search"),
    # M9 §2.12：认证（prefix=/v1/auth，/api 由 create_app 加）
    ("GET", "/api/v1/auth/setup-status"),
    ("POST", "/api/v1/auth/initialize"),
    ("POST", "/api/v1/auth/login/local"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/providers"),
    ("GET", "/api/v1/auth/me"),
    ("PATCH", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/change-password"),
}

STREAMING_PATHS = {
    "/api/threads/{thread_id}/runs/stream",
    "/api/threads/{thread_id}/runs/{run_id}/join",
    "/api/threads/{thread_id}/artifacts/{path:path}",
}


def _iter_routes(application: FastAPI):
    """Included routers keep their own prefix; create_app adds /api on the include."""
    for route in application.routes:
        if type(route).__name__ == "_IncludedRouter":
            prefix = getattr(route.include_context, "prefix", "") or ""
            for child in route.original_router.routes:
                path = child.path
                if prefix and not path.startswith(prefix):
                    path = prefix.rstrip("/") + path
                child.mounted_path = path
                yield child
            continue
        yield route


def _routes(application: FastAPI) -> set[tuple[str, str]]:
    """Business routes only. Framework docs stay out of the interface inventory."""
    found: set[tuple[str, str]] = set()
    for route in _iter_routes(application):
        path = getattr(route, "mounted_path", None) or getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.add((method, path))
    return found


def test_route_inventory_matches_m3() -> None:
    actual = _routes(create_app())
    assert actual == EXPECTED_ROUTES, (
        f"added: {sorted(actual - EXPECTED_ROUTES)}\n"
        f"removed: {sorted(EXPECTED_ROUTES - actual)}"
    )


def test_business_routes_declare_a_response_model() -> None:
    bare = [
        route.path
        for route in _iter_routes(create_app())
        if getattr(route, "mounted_path", route.path).startswith("/api")
        and getattr(route, "mounted_path", route.path) not in STREAMING_PATHS
        # A 204 route cannot carry a response body, so it has no response model.
        and getattr(route, "status_code", 200) not in {204, 304}
        and getattr(route, "response_model", None) is None
    ]
    assert bare == []


def test_slash_redirect_is_disabled() -> None:
    assert gateway.router.redirect_slashes is False
    assert create_app().router.redirect_slashes is False


def test_cors_exposes_stream_headers() -> None:
    exposed = []
    for middleware in create_app().user_middleware:
        exposed.extend(middleware.kwargs.get("expose_headers") or [])
    assert "Content-Location" in exposed
    assert "X-Trace-Id" in exposed


def test_routers_do_not_schedule_or_serialize() -> None:
    forbidden_calls = {"create_task", "dumps"}
    for path in ROUTER_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in forbidden_calls:
                raise AssertionError(f"{path}:{node.lineno} router called {node.func.attr}()")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "app"
                and isinstance(node.value, ast.Name)
                and node.value.id == "request"
            ):
                raise AssertionError(f"{path}:{node.lineno} router reads request.app")


class _Checkpointer:
    def __init__(self) -> None:
        self.saved: dict[tuple[str, str | None], SimpleNamespace] = {}

    async def aget_tuple(self, config: dict):
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is not None:
            return self.saved.get((thread_id, checkpoint_id))
        return self.saved.get((thread_id, None))


class _RunManager:
    def __init__(self) -> None:
        self.records: dict[str, RunRecord] = {}
        self.cancelled: list[tuple[str, str]] = []

    async def get(self, run_id: str, *, user_id: str | None) -> RunRecord | None:
        record = self.records.get(run_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            return None
        return record

    async def cancel(self, run_id: str, action: str = "interrupt") -> CancelOutcome:
        self.cancelled.append((run_id, action))
        record = self.records.get(run_id)
        if record is None:
            return CancelOutcome.unknown
        return CancelOutcome.cancelled


def _record(run_id: str = "run-1", thread_id: str = "thread-1", **overrides) -> RunRecord:
    fields = dict(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id="lead_agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        metadata={"topic": "m3"},
        kwargs={"input": {"messages": ["hi"]}},
        created_at="2026-10-04T00:00:00Z",
        updated_at="2026-10-04T00:00:00Z",
    )
    fields.update(overrides)
    return RunRecord(**fields)


def _services() -> SimpleNamespace:
    checkpointer = _Checkpointer()
    manager = _RunManager()
    return SimpleNamespace(
        run_manager=manager,
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(
            thread_store=MemoryThreadMetaStore(),
            checkpointer=checkpointer,
        ),
        run_store=SimpleNamespace(),
        event_store=SimpleNamespace(),
        checkpointer=checkpointer,
        manager=manager,
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


def test_unknown_thread_field_is_422_with_trace() -> None:
    response = _client(_services()).post("/api/threads", json={"thread_id": "thread-1", "nope": True})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["detail"] == "Request validation failed"
    assert body["errors"]
    assert response.headers["X-Trace-Id"]
    assert body["trace_id"] == response.headers["X-Trace-Id"]


def test_illegal_thread_id_is_422() -> None:
    response = _client(_services()).post("/api/threads/not.ok/runs", json={"assistant_id": "lead_agent"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_trailing_slash_is_404() -> None:
    response = _client(_services()).post("/api/threads/", json={})

    assert response.status_code == 404


def test_create_thread_returns_201() -> None:
    response = _client(_services()).post("/api/threads", json={"metadata": {"topic": "m3"}})

    assert response.status_code == 201
    body = response.json()
    assert body["thread_id"]
    assert body["metadata"] == {"topic": "m3"}
    assert body["status"] == "idle"
    assert body["display_name"] is None
    assert "user_id" not in body


def test_state_of_missing_thread_is_404() -> None:
    response = _client(_services()).get("/api/threads/missing/state")

    assert response.status_code == 404
    assert response.json()["code"] == "thread_not_found"


def test_state_returns_checkpoint_values_without_internal_keys() -> None:
    services = _services()
    created = _client(services).post("/api/threads", json={"thread_id": "thread-1"})
    assert created.status_code == 201
    services.checkpointer.saved[("thread-1", None)] = SimpleNamespace(
        checkpoint={
            "channel_values": {
                "messages": [{"type": "human", "content": "hi"}],
                "__pregel_tasks": [{"id": "hidden"}],
            }
        },
        config={"configurable": {"checkpoint_id": "cp-1"}},
    )

    response = _client(services).get("/api/threads/thread-1/state")

    assert response.status_code == 200
    assert response.json() == {
        "values": {"messages": [{"type": "human", "content": "hi"}]},
        "checkpoint_id": "cp-1",
        "checkpoint_ns": "",
    }


def test_missing_checkpoint_id_is_404() -> None:
    services = _services()
    assert _client(services).post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201

    response = _client(services).get("/api/threads/thread-1/state", params={"checkpoint_id": "missing"})

    assert response.status_code == 404
    assert response.json()["code"] == "checkpoint_not_found"


def test_create_run_returns_202_and_forwards_idempotency_key(monkeypatch) -> None:
    services = _services()
    seen: dict = {}

    async def start_run(body, *, thread_id, caller, services, idempotency_key=None, **kwargs):
        seen.update(thread_id=thread_id, key=idempotency_key, assistant_id=body.assistant_id, caller=caller)
        return RunLaunch(record=_record(), reused=False)

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    response = _client(services).post(
        "/api/threads/thread-1/runs",
        json={"assistant_id": "lead_agent", "input": {"messages": ["hi"]}},
        headers={"Idempotency-Key": "intent-1"},
    )

    assert response.status_code == 202
    assert response.json()["run_id"] == "run-1"
    assert response.json()["status"] == "pending"
    assert seen == {
        "thread_id": "thread-1",
        "key": "intent-1",
        "assistant_id": "lead_agent",
        "caller": seen["caller"],
    }
    assert isinstance(seen["caller"], RunCaller)


def test_stream_route_is_not_captured_as_a_run_id(monkeypatch) -> None:
    services = _services()
    seen: dict = {}

    async def start_run(body, *, thread_id, caller, services, idempotency_key=None, **kwargs):
        seen["thread_id"] = thread_id
        return RunLaunch(record=_record(thread_id=thread_id), reused=False)

    def sse_response(services, launch, request, *, apply_on_disconnect=True):
        seen["disconnect"] = apply_on_disconnect
        from fastapi.responses import StreamingResponse

        return StreamingResponse(iter(["event: end\ndata: null\n\n"]), media_type="text/event-stream")

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    monkeypatch.setattr(thread_runs, "sse_response", sse_response)
    response = _client(services).post(
        "/api/threads/thread-1/runs/stream",
        json={"assistant_id": "lead_agent"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert seen["thread_id"] == "thread-1"
    assert seen["disconnect"] is True


def test_wait_returns_the_snapshot_after_the_run_ends(monkeypatch) -> None:
    services = _services()
    services.checkpointer.saved[("thread-1", "cp-end")] = SimpleNamespace(
        checkpoint={"channel_values": {"title": "done"}},
        config={"configurable": {"checkpoint_id": "cp-end", "checkpoint_ns": ""}},
    )
    waited: list[str] = []

    async def start_run(body, *, thread_id, caller, services, idempotency_key=None, **kwargs):
        return RunLaunch(record=_record(thread_id=thread_id, metadata={"checkpoint_id": "cp-end"}), reused=False)

    async def wait_for_run_completion(services, record, request, *, apply_on_disconnect=True):
        waited.append(record.run_id)
        assert apply_on_disconnect is True
        return record

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    monkeypatch.setattr(thread_runs, "wait_for_run_completion", wait_for_run_completion)
    response = _client(services).post("/api/threads/thread-1/runs/wait", json={"assistant_id": "lead_agent"})

    assert response.status_code == 200
    assert waited == ["run-1"]
    assert response.json()["checkpoint_id"] == "cp-end"
    assert response.json()["values"]["title"] == "done"
    assert response.json()["run_id"] == "run-1"
    assert response.json()["status"] == "pending"


def test_domain_error_from_start_run_uses_the_error_body(monkeypatch) -> None:
    from backend.app.gateway.services import ThreadBusy

    async def start_run(*args, **kwargs):
        raise ThreadBusy("thread is busy")

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    response = _client(_services()).post("/api/threads/thread-1/runs", json={"assistant_id": "lead_agent"})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "thread_busy"
    assert body["detail"] == "thread is busy"
    assert body["trace_id"] == response.headers["X-Trace-Id"]
    assert "Traceback" not in response.text


def test_unexpected_error_hides_internals(monkeypatch) -> None:
    async def start_run(*args, **kwargs):
        raise RuntimeError("SELECT secret FROM users")

    monkeypatch.setattr(thread_runs, "start_run", start_run)
    response = _client(_services()).post("/api/threads/thread-1/runs", json={"assistant_id": "lead_agent"})

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["detail"] == "Internal server error"
    assert "secret" not in response.text
    assert body["trace_id"] == response.headers["X-Trace-Id"]


def test_cancel_scope_is_enforced(monkeypatch) -> None:
    from backend.app.gateway.run_models import CallerKind

    def limited(_request):
        return RunCaller(kind=CallerKind.HTTP, user_id="pat", scopes=frozenset({"runs:read"}))

    monkeypatch.setattr("backend.app.gateway.deps.caller_from_request", limited)
    response = _client(_services()).post("/api/threads/thread-1/runs/run-1/cancel", json={})

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
