from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from agentkit.contextforge.models import ContextForgeNotFound, ContextForgeTool, ToolWrite
from agentkit.contextforge.ownership import owner_from_annotations
from backend.app.gateway import tools_deps
from backend.app.gateway.auth.result import AuthKind, AuthResult
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

@pytest.mark.asyncio
async def test_status_keeps_channels_for_the_management_page(monkeypatch) -> None:
    class FakeRuntime:
        def status(self):
            return {
                "enabled": True,
                "reachable": True,
                "auth_ok": True,
                "admin_events": "connected",
                "sync": "ready",
                "index": "ready",
                "indexed_tools": 12,
                "total_tools": 13,
                "channels": {"keyword": "ready", "embedding": "ready", "rerank": "degraded"},
            }

    monkeypatch.setattr(tools_router, "get_contextforge_runtime", lambda: FakeRuntime())
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")
    response = await tools_router.tool_status(_request(), caller)
    assert response.indexed_tools == 12
    assert response.channels.keyword == "ready"
    assert response.channels.rerank == "degraded"

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