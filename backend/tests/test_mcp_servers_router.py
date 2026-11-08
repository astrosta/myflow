"""MCP server API: wire masking, owner-scoped CRUD and error mapping.

These tests mount the router on a bare FastAPI app with a fake service, the same
way test_tools_router.py does. They pin what the browser can see: no ciphertext or
raw header value ever appears in a response, PUT cannot rename a server, a server
owned by somebody else is a 404, and the tools endpoint is read-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from agentkit.mcp.errors import McpServerNotFound
from agentkit.mcp.service import MASK
from backend.app.gateway import mcp_servers_deps
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.errors import install_error_handlers
from backend.app.gateway.mcp_servers_wire import mask_headers
from backend.app.gateway.routers import mcp_servers as router_module
from backend.app.gateway.run_models import CallerKind, RunCaller


class FakeSync:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def remove_server_catalog(self, server_id: str) -> None:
        self.removed.append(server_id)


class FakeService:
    """A narrow service double with the real owner scoping and masking rules."""

    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {
            "s1": SimpleNamespace(
                id="s1",
                owner_user_id="u1",
                name="anysearch",
                display_name="AnySearch",
                description="实时搜索",
                url="https://api.anysearch.com/mcp",
                transport="streamable_http",
                headers_encrypted="gAAAAA-ciphertext",
                verify_tls=True,
                enabled=True,
                status="active",
                reachable=True,
                last_error=None,
                last_connected_at=None,
                last_tools_sync_at=None,
                created_at=None,
                updated_at=None,
            )
        }
        self.headers: dict[str, dict[str, str]] = {
            "s1": {"Authorization": "Bearer raw-secret", "X-Env": "prod"}
        }
        self.tools: list[SimpleNamespace] = [
            SimpleNamespace(
                id="t1",
                server_id="s1",
                owner_user_id="u1",
                name="search",
                call_name="mcp__anysearch__search",
                display_name="Search",
                description="search things",
                parameters=["q"],
                input_schema={"type": "object"},
                callable=True,
                enabled=True,
            )
        ]
        self.sync = FakeSync()
        self.created: list[Any] = []
        self.updated: list[Any] = []
        self.deleted: list[str] = []
        self.enabled_calls: list[tuple[str, bool]] = []
        self.refreshed: list[str] = []
        self.reindexed: list[str] = []

    async def list_servers(self, owner_user_id: str) -> list[Any]:
        return [r for r in self.records.values() if r.owner_user_id == owner_user_id]

    async def get_server(self, server_id: str, owner_user_id: str) -> Any:
        record = self.records.get(server_id)
        if record is None or record.owner_user_id != owner_user_id:
            raise McpServerNotFound("MCP server not found")
        return record

    async def create_server(self, owner_user_id: str, data: Any) -> Any:
        self.created.append(data)
        record = SimpleNamespace(
            id="new",
            owner_user_id=owner_user_id,
            name=data.name,
            display_name=data.display_name,
            description=data.description,
            url=data.url,
            transport=data.transport,
            headers_encrypted="gAAAAA-new-ciphertext",
            verify_tls=data.verify_tls,
            enabled=data.enabled,
            status="pending",
            reachable=False,
            last_error=None,
            last_connected_at=None,
            last_tools_sync_at=None,
            created_at=None,
            updated_at=None,
        )
        self.records["new"] = record
        self.headers["new"] = dict(data.headers or {})
        return record

    async def update_server(self, server_id: str, owner_user_id: str, data: Any) -> Any:
        record = await self.get_server(server_id, owner_user_id)
        self.updated.append(data)
        if data.display_name is not None and not hasattr(data.display_name, "__slots__"):
            record.display_name = data.display_name
        return record

    async def delete_server(self, server_id: str, owner_user_id: str) -> bool:
        await self.get_server(server_id, owner_user_id)
        self.deleted.append(server_id)
        self.records.pop(server_id, None)
        await self.sync.remove_server_catalog(server_id)
        return True

    async def set_enabled(self, server_id: str, owner_user_id: str, enabled: bool) -> Any:
        record = await self.get_server(server_id, owner_user_id)
        self.enabled_calls.append((server_id, enabled))
        record.enabled = enabled
        record.status = "pending" if enabled else "disabled"
        return record

    async def refresh_server(self, server_id: str, owner_user_id: str) -> Any:
        record = await self.get_server(server_id, owner_user_id)
        self.refreshed.append(server_id)
        record.status = "active"
        record.reachable = True
        return record

    async def list_tools(self, server_id: str, owner_user_id: str) -> list[Any]:
        await self.get_server(server_id, owner_user_id)
        return [t for t in self.tools if t.server_id == server_id]

    async def status(self, owner_user_id: str) -> dict[str, Any]:
        records = await self.list_servers(owner_user_id)
        return {
            "enabled": True,
            "servers_total": len(records),
            "servers_active": sum(1 for r in records if r.status == "active"),
            "servers_pending": sum(1 for r in records if r.status == "pending"),
            "servers_error": sum(1 for r in records if r.status == "error"),
            "servers_disabled": sum(1 for r in records if r.status == "disabled"),
            "servers_reachable": sum(1 for r in records if r.reachable),
            "tools_total": len(self.tools),
            "indexed_tools": len(self.tools),
            "last_sync_at": None,
            "last_error": None,
        }

    async def reindex(self, owner_user_id: str, reason: str = "manual") -> dict[str, Any]:
        self.reindexed.append(reason)
        records = await self.list_servers(owner_user_id)
        return {
            "scanned": len(records),
            "changed": len(records),
            "upserted": len(records),
            "deleted": 0,
            "duration_ms": 5,
            "error": None,
        }

    def masked_headers(self, record: Any) -> dict[str, str]:
        return mask_headers(self.headers.get(record.id, {}))


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def client(service: FakeService) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router, prefix="/api")
    app.state.mcp_server_service = service
    app.dependency_overrides[mcp_servers_deps.get_mcp_server_service] = lambda: service
    app.dependency_overrides[mcp_servers_deps.require_mcp_reader] = lambda: RunCaller(
        kind=CallerKind.HTTP, user_id="u1", user_role="user"
    )
    app.dependency_overrides[mcp_servers_deps.require_mcp_writer] = lambda: RunCaller(
        kind=CallerKind.HTTP, user_id="u1", user_role="user"
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Scope guard
# --------------------------------------------------------------------------- #


def _request() -> Request:
    app = FastAPI()
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/mcp-servers",
            "query_string": b"",
            "headers": [],
            "app": app,
        }
    )


def test_session_caller_passes_the_read_scope_guard() -> None:
    request = _request()
    request.state.auth = AuthResult(
        kind=AuthKind.session,
        user_id="u1",
        identity="user-1",
        role="user",
    )

    caller = mcp_servers_deps.require_mcp_servers_scope("mcp_servers:read")(request)

    assert caller.user_id == "u1"
    assert caller.allows("admin") is False


def test_pat_without_the_mcp_scope_is_denied() -> None:
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
        mcp_servers_deps.require_mcp_servers_scope("mcp_servers:read")(request)


# --------------------------------------------------------------------------- #
# Read surfaces never leak ciphertext or raw headers
# --------------------------------------------------------------------------- #


def test_list_never_serializes_the_encrypted_headers(
    client: TestClient, service: FakeService
) -> None:
    response = client.get("/api/mcp-servers")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    server = body["servers"][0]
    assert server["name"] == "anysearch"
    assert server["tool_count"] == 1
    assert "headers" not in server
    assert "headers_encrypted" not in response.text
    assert "gAAAAA-ciphertext" not in response.text


def test_detail_masks_headers_and_never_returns_the_ciphertext(
    client: TestClient,
) -> None:
    response = client.get("/api/mcp-servers/s1")

    assert response.status_code == 200
    body = response.json()
    assert body["headers"]["Authorization"] == MASK
    assert body["headers"]["X-Env"] == "prod"
    assert body["tool_count"] == 1
    assert body["status"] == "active"
    assert "gAAAAA-ciphertext" not in response.text
    assert "raw-secret" not in response.text


def test_another_owners_server_is_a_404(
    client: TestClient, service: FakeService
) -> None:
    response = client.get("/api/mcp-servers/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "mcp_server_not_found"


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def test_create_returns_the_masked_detail_and_never_the_ciphertext(
    client: TestClient, service: FakeService
) -> None:
    response = client.post(
        "/api/mcp-servers",
        json={
            "name": "brand-new",
            "display_name": "Brand New",
            "url": "https://mcp.example.com/v1/mcp",
            "transport": "streamable_http",
            "headers": {"Authorization": "Bearer top-secret"},
            "verify_tls": True,
            "enabled": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "new"
    assert body["headers"]["Authorization"] == MASK
    assert "gAAAAA-new-ciphertext" not in response.text
    assert "top-secret" not in response.text
    assert service.created[0].name == "brand-new"


def test_put_cannot_change_the_name(client: TestClient, service: FakeService) -> None:
    rejected = client.put(
        "/api/mcp-servers/s1",
        json={"name": "renamed", "display_name": "Renamed"},
    )

    assert rejected.status_code == 422
    assert service.updated == []

    accepted = client.put(
        "/api/mcp-servers/s1",
        json={"display_name": "Renamed"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["display_name"] == "Renamed"
    assert service.updated[0].name is None


def test_delete_removes_the_server_and_its_catalog(
    client: TestClient, service: FakeService
) -> None:
    response = client.delete("/api/mcp-servers/s1")

    assert response.status_code == 204
    assert service.deleted == ["s1"]
    assert service.sync.removed == ["s1"]


def test_state_and_refresh_return_the_refreshed_detail(
    client: TestClient, service: FakeService
) -> None:
    disabled = client.post("/api/mcp-servers/s1/state", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert service.enabled_calls == [("s1", False)]

    refreshed = client.post("/api/mcp-servers/s1/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "active"
    assert service.refreshed == ["s1"]


def test_state_model_forbids_extra_fields(client: TestClient) -> None:
    response = client.post(
        "/api/mcp-servers/s1/state", json={"enabled": True, "status": "active"}
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Tools are read-only summaries
# --------------------------------------------------------------------------- #


def test_tools_returns_summaries_only(client: TestClient) -> None:
    response = client.get("/api/mcp-servers/s1/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    tool = body["tools"][0]
    assert tool["name"] == "search"
    assert tool["call_name"] == "mcp__anysearch__search"
    assert tool["parameters"] == ["q"]
    assert tool["callable"] is True
    assert tool["enabled"] is True
    assert "input_schema" not in tool
    assert "owner_user_id" not in tool
    assert "content" not in tool


# --------------------------------------------------------------------------- #
# Static routes must win over /{server_id}
# --------------------------------------------------------------------------- #


def test_status_is_scoped_to_the_caller(client: TestClient) -> None:
    response = client.get("/api/mcp-servers/status")

    assert response.status_code == 200
    body = response.json()
    assert body["servers_total"] == 1
    assert body["tools_total"] == 1


def test_reindex_is_a_separate_static_route(
    client: TestClient, service: FakeService
) -> None:
    response = client.post("/api/mcp-servers/reindex")

    assert response.status_code == 200
    assert response.json()["scanned"] == 1
    assert service.reindexed == ["manual"]
