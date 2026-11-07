from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from agentkit.contextforge.models import ContextForgeNotFound, ContextForgeTool, ToolWrite
from agentkit.contextforge.ownership import owner_from_annotations
from agentkit.contextforge.sync import ContextForgeSyncStats
from backend.app.gateway import tools_deps
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.errors import install_error_handlers
from backend.app.gateway.run_models import CallerKind, RunCaller
from backend.app.gateway.routers import tools as tools_router


class FakeAdmin:
    async def list_tools(self, *, limit: int, skip: int, include_inactive: bool):
        assert include_inactive is True
        return [
            ContextForgeTool(
                id="t1",
                name="github_search",
                display_name="GitHub",
                description="search code",
                enabled=True,
                reachable=True,
                headers={"Authorization": "Bearer secret"},
                input_schema={"type": "object"},
            )
        ]

    async def get_tool(self, tool_id: str):
        assert tool_id == "t1"
        return ContextForgeTool(
            id="t1",
            name="github_search",
            description="search code",
            headers={"Authorization": "Bearer secret"},
            input_schema={"type": "object"},
        )


class OwnedFakeAdmin(FakeAdmin):
    async def list_tools(self, *, limit: int, skip: int, include_inactive: bool):
        del limit, skip, include_inactive
        return [
            ContextForgeTool(id="mine", name="mine", owner="u1", annotations={"myflow_owner": "u1"}),
            ContextForgeTool(id="other", name="other", owner="u2", annotations={"myflow_owner": "u2"}),
            ContextForgeTool(id="shared", name="shared"),
        ]

    async def get_tool(self, tool_id: str):
        for tool in await self.list_tools(limit=50, skip=0, include_inactive=True):
            if tool.id == tool_id:
                return tool
        raise AssertionError(tool_id)


def _request(admin: object | None = None) -> Request:
    app = FastAPI()
    app.state.contextforge_admin_client = admin or FakeAdmin()
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/tools",
            "query_string": b"",
            "headers": [],
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_list_tools_returns_summaries_without_headers(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")
    response = await tools_router.list_tools(
        _request(),
        caller,
        None,
        50,
        None,
        None,
        None,
    )
    assert response.count == 1
    assert response.tools[0].name == "github_search"
    assert not hasattr(response.tools[0], "headers")


@pytest.mark.asyncio
async def test_get_tool_masks_headers(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")
    response = await tools_router.get_tool("t1", _request(), caller)
    assert response.headers["Authorization"] == "****"
    assert response.input_schema == {"type": "object"}

class FakeRuntime:
    """The runtime double, with the real shape: `status` is async and scoped.

    `status` reads the authoritative store in Task 5, so a synchronous double
    would hide the bug the route had (a coroutine handed to
    `model_validate`), and an unscoped double would hide the owner leak.
    """

    def __init__(self, *, stats: ContextForgeSyncStats | None = None) -> None:
        self.owners: list[str | None] = []
        self.reindexed = 0
        self.stats = stats or ContextForgeSyncStats(
            scanned=3, changed=1, upserted=1, deleted=0, duration_ms=42
        )

    async def status(self, owner_user_id: str | None = None) -> dict[str, object]:
        self.owners.append(owner_user_id)
        scoped = owner_user_id is not None
        return {
            "enabled": True,
            "reachable": True,
            "auth_ok": True,
            "admin_events": "connected",
            "daily": "running",
            "sync": "ready",
            "index": "ready",
            "indexed_tools": 2 if scoped else 12,
            "total_tools": 2 if scoped else 13,
            "last_sync_at": None,
            "last_sync_reason": "startup",
            "last_error": None,
            "channels": {"keyword": "ready", "embedding": "ready", "rerank": "degraded"},
        }

    async def reindex(self) -> ContextForgeSyncStats:
        self.reindexed += 1
        return self.stats


@pytest.mark.asyncio
async def test_status_keeps_channels_for_the_management_page(monkeypatch) -> None:
    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: FakeRuntime())
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")

    response = await tools_router.tool_status(_request(), caller)

    assert response.channels.keyword == "ready"
    assert response.channels.rerank == "degraded"
    # The midnight task state is part of the response now, not a dropped key.
    assert response.daily == "running"


@pytest.mark.asyncio
async def test_status_counts_are_scoped_to_the_caller(monkeypatch) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: runtime)
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    response = await tools_router.tool_status(_request(), caller)

    assert runtime.owners == ["u1"], "status must be asked for the caller's rows"
    assert (response.indexed_tools, response.total_tools) == (2, 2)


@pytest.mark.asyncio
async def test_status_without_a_runtime_reports_disabled(monkeypatch) -> None:
    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: None)
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    response = await tools_router.tool_status(_request(), caller)

    assert response.enabled is False
    assert response.daily == "disabled"


@pytest.mark.asyncio
async def test_reindex_reports_the_duration_the_sync_measured(monkeypatch) -> None:
    runtime = FakeRuntime(
        stats=ContextForgeSyncStats(scanned=4, changed=2, upserted=2, duration_ms=37)
    )
    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: runtime)
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")

    body = await tools_router.reindex_tools(_request(), caller)

    assert body == {
        "scanned": 4,
        "changed": 2,
        "upserted": 2,
        "deleted": 0,
        "duration_ms": 37,
        "error": None,
    }


@pytest.mark.asyncio
async def test_reindex_without_a_runtime_is_a_coded_503(monkeypatch) -> None:
    from backend.app.gateway.errors import ContextForgeDisabled

    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: None)
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")

    with pytest.raises(ContextForgeDisabled) as caught:
        await tools_router.reindex_tools(_request(), caller)

    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_search_says_unavailable_instead_of_returning_no_hits() -> None:
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    with pytest.raises(tools_router.ToolSearchUnavailable) as caught:
        await tools_router.search_tools_preview(_request(), caller, "github", 5)

    assert caught.value.status_code == 503
    assert caught.value.code == "tool_search_unavailable"


def test_the_search_503_reaches_the_client_as_a_coded_error() -> None:
    """The route raises; the registered handler produces the shared error body."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/tools/search")
    async def probe() -> dict[str, object]:
        raise tools_router.ToolSearchUnavailable("tool search is not available")

    response = TestClient(app).get("/api/tools/search")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "tool_search_unavailable"
    assert body["detail"]
    assert body["trace_id"]

def test_regular_authenticated_user_can_use_the_tools_proxy() -> None:
    request = _request()
    request.state.auth = AuthResult(
        kind=AuthKind.session,
        user_id="u1",
        identity="user-1",
        role="user",
    )

    caller = tools_deps.require_tools_scope("tools:read")(request)

    assert caller.user_id == "u1"
    assert caller.allows("admin") is False

def test_pat_without_tools_scope_is_denied() -> None:
    from backend.app.gateway.services import RunPermissionDenied

    request = _request()
    request.state.auth = AuthResult(
        kind=AuthKind.pat,
        user_id="u1",
        identity="user-1",
        role="user",
        scopes=frozenset({"models:read"}),
    )

    with pytest.raises(RunPermissionDenied):
        tools_deps.require_tools_scope("tools:read")(request)

@pytest.mark.asyncio
async def test_list_hides_other_users_tools_and_marks_unowned_claimable(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    response = await tools_router.list_tools(
        _request(OwnedFakeAdmin()),
        caller,
        None,
        50,
        None,
        None,
        None,
    )

    by_name = {tool.name: tool for tool in response.tools}
    assert set(by_name) == {"mine", "shared"}
    assert by_name["mine"].owned is True
    assert by_name["shared"].claimable is True
    assert by_name["shared"].can_edit is False


@pytest.mark.asyncio
async def test_get_another_users_tool_is_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    with pytest.raises(ContextForgeNotFound):
        await tools_router.get_tool("other", _request(OwnedFakeAdmin()), caller)

class RecordingOwnedAdmin(OwnedFakeAdmin):
    def __init__(self) -> None:
        self.created: ToolWrite | None = None
        self.updated: ToolWrite | None = None

    async def create_tool(self, data: ToolWrite):
        self.created = data
        return ContextForgeTool(
            id="new",
            name=data.name,
            annotations=data.annotations,
            owner=owner_from_annotations(data.annotations),
        )

    async def update_tool(self, tool_id: str, data: ToolWrite):
        self.updated = data
        return ContextForgeTool(
            id=tool_id,
            name=data.name,
            annotations=data.annotations,
            owner=owner_from_annotations(data.annotations),
        )


@pytest.mark.asyncio
async def test_create_stamps_the_calling_user_as_owner(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    admin = RecordingOwnedAdmin()
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    response = await tools_router.create_tool(
        ToolWrite(name="new_tool", description="d"),
        _request(admin),
        caller,
    )

    assert admin.created is not None
    assert admin.created.annotations["myflow_owner"] == "u1"
    assert response.owned is True


@pytest.mark.asyncio
async def test_claim_assigns_an_unowned_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        tools_deps,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    admin = RecordingOwnedAdmin()
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u1", user_role="user")

    response = await tools_router.claim_tool("shared", _request(admin), caller)

    assert admin.updated is not None
    assert admin.updated.annotations["myflow_owner"] == "u1"
    assert response.owned is True