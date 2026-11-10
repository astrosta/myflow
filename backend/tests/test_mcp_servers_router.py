"""MCP server API: wire masking, owner-scoped CRUD and error mapping.

These tests mount the router on a bare FastAPI app with a fake service, the same
way test_tools_router.py does. They pin what the browser can see: no ciphertext,
raw header value, or URL query string ever appears in a response; PUT cannot
rename a server or null out verify_tls; a server owned by somebody else is a 404;
and the tools endpoint is read-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from agentkit.mcp.errors import (
    McpServerConfigurationError,
    McpServerConflict,
    McpServerNotFound,
    McpServerValidationError,
)
from agentkit.mcp.service import (
    MASK,
    McpServerReindexStats,
    mask_headers,
    restore_masked_headers,
)
from backend.app.gateway import mcp_servers_deps
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.errors import install_error_handlers
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
                url="https://api.anysearch.com/mcp?tenant=acme&token=url-secret",
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
        self.reindex_owners: list[str] = []
        self.reindex_stats = McpServerReindexStats(
            scanned=2, changed=1, upserted=1, deleted=0, failed=0, duration_ms=5
        )
        self.conflict = False
        self.decrypt_error = False

    async def list_servers(self, owner_user_id: str) -> list[Any]:
        return [r for r in self.records.values() if r.owner_user_id == owner_user_id]

    async def get_server(self, server_id: str, owner_user_id: str) -> Any:
        record = self.records.get(server_id)
        if record is None or record.owner_user_id != owner_user_id:
            raise McpServerNotFound("MCP server not found")
        return record

    async def create_server(self, owner_user_id: str, data: Any) -> Any:
        if self.conflict:
            raise McpServerConflict("MCP server name is already used by this user")
        restore_masked_headers(data.headers or {}, {})
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
        if self.conflict:
            raise McpServerConflict("MCP server update conflicts")
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

    async def reindex(self, owner_user_id: str) -> McpServerReindexStats:
        self.reindex_owners.append(owner_user_id)
        return self.reindex_stats

    def masked_headers(self, record: Any) -> dict[str, str]:
        if self.decrypt_error:
            raise McpServerConfigurationError(
                "stored MCP server headers could not be decrypted with the configured "
                "encryption key"
            )
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


def _bare_client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router, prefix="/api")
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
# Read surfaces never leak ciphertext, raw headers or URL query strings
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
    assert server["url"] == "https://api.anysearch.com/mcp"
    assert server["url_has_query"] is True
    assert "headers" not in server
    assert "headers_encrypted" not in response.text
    assert "gAAAAA-ciphertext" not in response.text
    assert "url-secret" not in response.text


def test_detail_masks_headers_and_never_returns_the_ciphertext_or_query(
    client: TestClient,
) -> None:
    response = client.get("/api/mcp-servers/s1")

    assert response.status_code == 200
    body = response.json()
    # Every value is masked, not just the sensitive-looking names: a name
    # allowlist misses the next vendor's X-Custom-Token.
    assert body["headers"]["Authorization"] == MASK
    assert body["headers"]["X-Env"] == MASK
    assert "prod" not in response.text
    assert body["tool_count"] == 1
    assert body["status"] == "active"
    assert body["url"] == "https://api.anysearch.com/mcp"
    assert body["url_has_query"] is True
    assert "gAAAAA-ciphertext" not in response.text
    assert "raw-secret" not in response.text
    assert "url-secret" not in response.text


def test_another_owners_server_is_a_404(
    client: TestClient, service: FakeService
) -> None:
    response = client.get("/api/mcp-servers/missing")

    assert response.status_code == 404
    assert response.json()["code"] == "mcp_server_not_found"


def test_decrypt_failure_is_a_sanitized_500_not_a_422(
    client: TestClient, service: FakeService
) -> None:
    service.decrypt_error = True

    response = client.get("/api/mcp-servers/s1")

    assert response.status_code == 500
    assert response.json()["code"] == "mcp_server_configuration_error"
    assert "raw-secret" not in response.text


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
    assert body["url_has_query"] is False
    assert "gAAAAA-new-ciphertext" not in response.text
    assert "top-secret" not in response.text
    assert service.created[0].name == "brand-new"


def test_create_rejects_a_literal_mask_header(
    client: TestClient, service: FakeService
) -> None:
    response = client.post(
        "/api/mcp-servers",
        json={
            "name": "masked",
            "url": "https://mcp.example.com/v1/mcp",
            "headers": {"Authorization": MASK},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "mcp_server_invalid"
    assert service.created == []


def test_create_conflict_is_a_409(
    client: TestClient, service: FakeService
) -> None:
    service.conflict = True

    response = client.post(
        "/api/mcp-servers",
        json={"name": "duplicate", "url": "https://mcp.example.com/v1/mcp"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "mcp_server_conflict"


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


def test_put_rejects_explicit_null_verify_tls(
    client: TestClient, service: FakeService
) -> None:
    response = client.put(
        "/api/mcp-servers/s1",
        json={"verify_tls": None},
    )

    assert response.status_code == 422
    assert service.updated == []


def test_put_conflict_is_a_409(client: TestClient, service: FakeService) -> None:
    service.conflict = True

    response = client.put(
        "/api/mcp-servers/s1",
        json={"display_name": "Conflicting"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "mcp_server_conflict"


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
# Tools are read-only summaries; the stored schema is opt-in
# --------------------------------------------------------------------------- #

#: A stored inputSchema that must come back verbatim: mcp_tools.input_schema keeps
#: $ref/anyOf/default exactly as the server sent them, and the eval dataset
#: builder derives its sample arguments from this object.
REF_SCHEMA = {
    "$defs": {"Window": {"type": "object", "properties": {"start": {"type": "string"}}}},
    "type": "object",
    "required": ["filter"],
    "properties": {
        "filter": {"anyOf": [{"$ref": "#/$defs/Window"}, {"type": "null"}]},
        "limit": {"type": "integer", "default": 10},
    },
    "additionalProperties": False,
}


def _schema_tool(
    name: str,
    call_name: str,
    input_schema: Any,
    *,
    callable: bool = True,
    enabled: bool = True,
) -> SimpleNamespace:
    """One mcp_tools row as the owner-scoped service hands it back."""
    return SimpleNamespace(
        id=f"tool-{name}",
        server_id="s1",
        owner_user_id="u1",
        name=name,
        call_name=call_name,
        display_name=name,
        description=f"{name} things",
        parameters=[],
        input_schema=input_schema,
        callable=callable,
        enabled=enabled,
    )


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


def test_tools_hides_the_stored_schema_by_default(
    client: TestClient, service: FakeService
) -> None:
    service.tools.append(_schema_tool("resolve", "mcp__anysearch__resolve", REF_SCHEMA))

    response = client.get("/api/mcp-servers/s1/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [tool["call_name"] for tool in body["tools"]] == [
        "mcp__anysearch__search",
        "mcp__anysearch__resolve",
    ]
    # Absent, not null: the browser payload must not grow for a UI that never
    # reads a schema.
    assert all("input_schema" not in tool for tool in body["tools"])
    assert "anyOf" not in response.text
    # An explicit false is byte-for-byte the omitted default.
    explicit = client.get("/api/mcp-servers/s1/tools?include_schema=false")
    assert explicit.text == response.text


def test_tools_returns_each_stored_schema_verbatim_when_include_schema_is_true(
    client: TestClient, service: FakeService
) -> None:
    service.tools.append(_schema_tool("resolve", "mcp__anysearch__resolve", REF_SCHEMA))
    service.tools.append(_schema_tool("ping", "mcp__anysearch__ping", {}))

    default = client.get("/api/mcp-servers/s1/tools").json()
    response = client.get("/api/mcp-servers/s1/tools?include_schema=true")

    assert response.status_code == 200
    body = response.json()
    # Same rows in the same order; the flag only adds the schema key.
    assert [tool["call_name"] for tool in body["tools"]] == [
        tool["call_name"] for tool in default["tools"]
    ]
    assert body["count"] == default["count"] == 3
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    schema = body["tools"][1]["input_schema"]
    assert schema == REF_SCHEMA
    # A dropped $ref or a flattened anyOf would silently change the arguments a
    # sample is built from, so pin the pieces a naive re-serialization loses.
    assert schema["properties"]["filter"]["anyOf"][0] == {"$ref": "#/$defs/Window"}
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["additionalProperties"] is False
    # An empty stored schema stays an empty object, never a repaired one.
    assert body["tools"][2]["input_schema"] == {}
    assert "owner_user_id" not in response.text
    assert "content" not in response.text


@pytest.mark.parametrize("stored", ["not-a-schema", ["type", "object"], 7])
def test_a_non_object_stored_schema_never_breaks_the_projection(
    client: TestClient, service: FakeService, stored: Any
) -> None:
    """One unreadable schema must not take the whole response down.

    mcp_tools.input_schema is a typed JSON column and the sync coerces a non-object
    to {}, so a row like this cannot be written today; it is defensive anyway,
    because the schema field is new here and a validation error would break even
    the schema-free UI call. Such a row is treated as carrying no schema -- not
    repaired, not echoed -- and the healthy rows still come back.
    """
    service.tools.append(_schema_tool("weird", "mcp__anysearch__weird", stored))

    plain = client.get("/api/mcp-servers/s1/tools")
    asked = client.get("/api/mcp-servers/s1/tools?include_schema=true")

    assert plain.status_code == 200
    assert asked.status_code == 200
    assert [tool["call_name"] for tool in plain.json()["tools"]] == [
        "mcp__anysearch__search",
        "mcp__anysearch__weird",
    ]
    assert all("input_schema" not in tool for tool in plain.json()["tools"])
    assert asked.json()["count"] == 2
    # The healthy row keeps its verbatim schema; the broken one simply has none.
    assert asked.json()["tools"][0]["input_schema"] == {"type": "object"}
    assert "input_schema" not in asked.json()["tools"][1]


def test_include_schema_is_not_an_authorization_shortcut(service: FakeService) -> None:
    from backend.app.gateway.services import RunPermissionDenied

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router, prefix="/api")
    app.state.mcp_server_service = service
    app.dependency_overrides[mcp_servers_deps.get_mcp_server_service] = lambda: service

    def _no_read_scope() -> RunCaller:
        raise RunPermissionDenied("missing scope: mcp_servers:read")

    app.dependency_overrides[mcp_servers_deps.require_mcp_reader] = _no_read_scope

    response = TestClient(app).get("/api/mcp-servers/s1/tools?include_schema=true")

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert "input_schema" not in response.text


# --------------------------------------------------------------------------- #
# Static routes must win over /{server_id}
# --------------------------------------------------------------------------- #


def test_status_is_scoped_to_the_caller(client: TestClient) -> None:
    response = client.get("/api/mcp-servers/status")

    assert response.status_code == 200
    body = response.json()
    assert body["servers_total"] == 1
    assert body["tools_total"] == 1


def test_reindex_returns_the_typed_sync_stats(
    client: TestClient, service: FakeService
) -> None:
    response = client.post("/api/mcp-servers/reindex")

    assert response.status_code == 200
    assert response.json() == {
        "scanned": 2,
        "changed": 1,
        "upserted": 1,
        "deleted": 0,
        "failed": 0,
        "duration_ms": 5,
        "error": None,
    }
    assert service.reindex_owners == ["u1"]


# --------------------------------------------------------------------------- #
# Status: the runtime's view, not only the service's row counts
# --------------------------------------------------------------------------- #


def _client_with_runtime(service: FakeService, runtime: Any) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router, prefix="/api")
    app.state.mcp_server_service = service
    app.dependency_overrides[mcp_servers_deps.get_mcp_server_service] = lambda: service
    app.dependency_overrides[mcp_servers_deps.get_mcp_runtime] = lambda: runtime
    app.dependency_overrides[mcp_servers_deps.require_mcp_reader] = lambda: RunCaller(
        kind=CallerKind.HTTP, user_id="u1", user_role="user"
    )
    app.dependency_overrides[mcp_servers_deps.require_mcp_writer] = lambda: RunCaller(
        kind=CallerKind.HTTP, user_id="u1", user_role="user"
    )
    return TestClient(app)


class FakeRuntime:
    def __init__(self, *, status: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        self._status = status or {}
        self._error = error
        self.owners: list[str] = []

    async def status(self, owner_user_id: str) -> dict[str, Any]:
        self.owners.append(owner_user_id)
        if self._error is not None:
            raise self._error
        # The real runtime only exposes the retry record on the catalog-wide view
        # (owner_user_id=None); every route call is owner-scoped, so this fake must
        # not invent fields the route can never receive.
        return {
            key: value
            for key, value in self._status.items()
            if key not in ("last_retry_at", "last_retry_tool", "last_retry_result")
        }


def test_status_prefers_the_runtime_view(service: FakeService) -> None:
    runtime = FakeRuntime(
        status={
            "enabled": True,
            "servers_total": 1,
            "servers_active": 1,
            "servers_pending": 0,
            "servers_error": 0,
            "servers_disabled": 0,
            "servers_reachable": 1,
            "tools_total": 4,
            "indexed_tools": 3,
            "last_sync_at": None,
            "last_error": None,
            "sync": "degraded",
            "index": "ready",
            "last_sync_reason": "server_refresh",
            "last_sync_duration_ms": 42,
        }
    )

    response = _client_with_runtime(service, runtime).get("/api/mcp-servers/status")

    assert response.status_code == 200
    body = response.json()
    assert runtime.owners == ["u1"]
    # The service projection reports indexed == total; the runtime knows better.
    assert body["indexed_tools"] == 3
    assert body["sync"] == "degraded"
    assert body["index"] == "ready"
    assert body["last_sync_reason"] == "server_refresh"
    assert body["last_sync_duration_ms"] == 42
    # The retry record is process-wide only; a scoped status must not promise it.
    assert "last_retry_at" not in body
    assert "last_retry_tool" not in body
    assert "last_retry_result" not in body


def test_status_falls_back_to_the_service_without_a_runtime(
    client: TestClient,
) -> None:
    response = client.get("/api/mcp-servers/status")

    assert response.status_code == 200
    body = response.json()
    assert body["servers_total"] == 1
    assert body["sync"] is None
    assert body["index"] is None
    assert body["last_sync_reason"] is None


def test_a_broken_runtime_status_degrades_to_the_service_view(
    service: FakeService,
) -> None:
    runtime = FakeRuntime(error=RuntimeError("status broke"))

    response = _client_with_runtime(service, runtime).get("/api/mcp-servers/status")

    assert response.status_code == 200
    assert response.json()["servers_total"] == 1


# --------------------------------------------------------------------------- #
# Write bounds: display_name length and the explicit query clear
# --------------------------------------------------------------------------- #


def test_create_rejects_a_display_name_longer_than_the_column(
    client: TestClient, service: FakeService
) -> None:
    response = client.post(
        "/api/mcp-servers",
        json={
            "name": "anysearch",
            "display_name": "x" * 256,
            "url": "https://api.anysearch.com/mcp",
            "headers": {},
            "verify_tls": True,
            "enabled": True,
        },
    )

    assert response.status_code == 422
    assert service.created == []


def test_put_rejects_a_display_name_longer_than_the_column(
    client: TestClient, service: FakeService
) -> None:
    response = client.put("/api/mcp-servers/s1", json={"display_name": "y" * 256})

    assert response.status_code == 422
    assert service.updated == []


def test_put_forwards_clear_query_to_the_service(
    client: TestClient, service: FakeService
) -> None:
    response = client.put("/api/mcp-servers/s1", json={"clear_query": True})

    assert response.status_code == 200
    assert service.updated[-1].clear_query is True


def test_missing_service_is_a_coded_503(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_servers_deps,
        "get_app_config",
        lambda: SimpleNamespace(
            mcp_servers=SimpleNamespace(enabled=False)
        ),
    )

    response = _bare_client().get("/api/mcp-servers")

    assert response.status_code == 503
    assert response.json()["code"] == "mcp_servers_disabled"
