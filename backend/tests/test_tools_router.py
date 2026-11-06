from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from agentkit.contextforge.models import ContextForgeTool
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


def _request() -> Request:
    app = FastAPI()
    app.state.contextforge_admin_client = FakeAdmin()
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
        tools_router,
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
        tools_router,
        "get_app_config",
        lambda: SimpleNamespace(contextforge=SimpleNamespace(enabled=True)),
    )
    caller = RunCaller(kind=CallerKind.HTTP, user_id="u", user_role="admin")
    response = await tools_router.get_tool("t1", _request(), caller)
    assert response.headers["Authorization"] == "****"
    assert response.input_schema == {"type": "object"}
