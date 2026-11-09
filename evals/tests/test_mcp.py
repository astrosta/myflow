"""MCP tools/list client tests.

Everything here is black-box HTTP: an httpx.MockTransport stands in for the MCP
server, so the eval package never talks to a real server. The header value used
below is a fixture, not a credential.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from myflow_evals.mcp import McpClient, McpServer, McpServerSecret, McpTool, load_server_secrets

SERVER_URL = "https://mcp.test/rpc"
SECRET_HEADER_VALUE = "fixture-header-value"
SEARCH_PROJECTS_SCHEMA = {
    "type": "object",
    "required": ["sector"],
    "properties": {"sector": {"type": "string", "enum": ["solar", "wind"]}},
}


def _server(url: str = SERVER_URL) -> McpServer:
    return McpServer(
        id="srv-1",
        url=url,
        headers={"X-Api-Key": SECRET_HEADER_VALUE},
        name="project_db",
    )


class _Rpc:
    """A fake MCP server: records JSON-RPC bodies and answers per method."""

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]],
        sse: bool = False,
        session_id: str | None = "sess-1",
        protocol_version: str | None = "2025-06-18",
    ) -> None:
        self._pages = list(pages)
        self._sse = sse
        self._session_id = session_id
        self._protocol_version = protocol_version
        self.calls: list[tuple[dict[str, Any], httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((body, request))
        method = body["method"]
        if method == "initialize":
            headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
            result: dict[str, Any] = {
                "capabilities": {},
                "serverInfo": {"name": "project_db"},
            }
            if self._protocol_version is not None:
                result["protocolVersion"] = self._protocol_version
            return httpx.Response(
                200,
                headers=headers,
                json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": self._pages.pop(0)}
            if not self._sse:
                return httpx.Response(200, json=payload)
            text = "event: message\ndata: " + json.dumps(payload) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
        raise AssertionError(f"unexpected MCP method {method!r}")

    @property
    def methods(self) -> list[str]:
        return [body["method"] for body, _ in self.calls]


def _client(rpc: _Rpc) -> McpClient:
    return McpClient(transport=httpx.MockTransport(rpc))


def test_list_tools_initializes_a_session_and_reads_input_schema() -> None:
    rpc = _Rpc(
        pages=[
            {
                "tools": [
                    {
                        "name": "search_projects",
                        "description": "Find projects",
                        "inputSchema": SEARCH_PROJECTS_SCHEMA,
                    }
                ]
            }
        ]
    )

    tools = _client(rpc).list_tools(_server())

    assert tools == [
        McpTool(
            call_name="mcp__project_db__search_projects",
            name="search_projects",
            description="Find projects",
            input_schema=SEARCH_PROJECTS_SCHEMA,
        )
    ]
    assert rpc.methods == ["initialize", "notifications/initialized", "tools/list"]

    initialize_body, initialize_request = rpc.calls[0]
    assert initialize_body["jsonrpc"] == "2.0"
    assert isinstance(initialize_body["id"], int)
    assert initialize_body["params"]["capabilities"] == {}
    assert initialize_body["params"]["protocolVersion"]
    assert initialize_body["params"]["clientInfo"]["name"] == "myflow-evals"
    assert "mcp-session-id" not in initialize_request.headers
    assert "mcp-protocol-version" not in initialize_request.headers

    initialized_body, initialized_request = rpc.calls[1]
    assert "id" not in initialized_body
    assert initialized_request.headers["mcp-session-id"] == "sess-1"
    assert initialized_request.headers["mcp-protocol-version"] == "2025-06-18"

    tools_body, tools_request = rpc.calls[2]
    assert tools_body["method"] == "tools/list"
    assert tools_body["params"] == {}
    assert tools_request.headers["mcp-session-id"] == "sess-1"
    assert tools_request.headers["mcp-protocol-version"] == "2025-06-18"

    for _, request in rpc.calls:
        assert request.method == "POST"
        assert str(request.url) == SERVER_URL
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["x-api-key"] == SECRET_HEADER_VALUE
        assert "authorization" not in request.headers


def test_list_tools_omits_the_protocol_version_header_the_server_never_returned() -> None:
    rpc = _Rpc(pages=[{"tools": [{"name": "first"}]}], protocol_version=None)

    _client(rpc).list_tools(_server())

    assert all("mcp-protocol-version" not in request.headers for _, request in rpc.calls)


def test_list_tools_parses_a_server_sent_event_response() -> None:
    rpc = _Rpc(
        sse=True,
        pages=[
            {
                "tools": [
                    {"name": "get_project", "description": "Load one", "inputSchema": {"type": "object"}}
                ]
            }
        ],
    )

    tools = _client(rpc).list_tools(_server())

    assert [tool.name for tool in tools] == ["get_project"]
    assert tools[0].call_name == "mcp__project_db__get_project"


def test_list_tools_follows_next_cursor() -> None:
    rpc = _Rpc(
        pages=[
            {"tools": [{"name": "first"}], "nextCursor": "cursor-2"},
            {"tools": [{"name": "second"}]},
        ]
    )

    tools = _client(rpc).list_tools(_server())

    assert [tool.name for tool in tools] == ["first", "second"]
    assert [body["params"] for body, _ in rpc.calls if body["method"] == "tools/list"] == [
        {},
        {"cursor": "cursor-2"},
    ]


def test_list_tools_drops_a_tool_without_a_name_and_defaults_a_missing_schema() -> None:
    rpc = _Rpc(
        pages=[
            {
                "tools": [
                    {"description": "no name"},
                    {"name": ""},
                    {"name": "keep_me", "description": 7},
                    "not an object",
                ]
            }
        ]
    )

    tools = _client(rpc).list_tools(_server())

    assert tools == [
        McpTool(call_name="mcp__project_db__keep_me", name="keep_me", description="", input_schema={})
    ]


def test_list_tools_works_without_a_session_header_or_auth_headers() -> None:
    rpc = _Rpc(pages=[{"tools": [{"name": "first"}]}], session_id=None)

    tools = _client(rpc).list_tools(McpServer(id="srv-2", url=SERVER_URL, headers={}, name="open_db"))

    assert [tool.call_name for tool in tools] == ["mcp__open_db__first"]
    assert all("mcp-session-id" not in request.headers for _, request in rpc.calls)


def test_list_tools_reports_a_jsonrpc_error_without_the_server_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "Method not found"},
            },
        )

    client = McpClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="Method not found") as excinfo:
        client.list_tools(_server("https://mcp.test/rpc?token=query-secret"))

    assert "query-secret" not in str(excinfo.value)
    assert SECRET_HEADER_VALUE not in str(excinfo.value)


def test_list_tools_sanitizes_an_http_error_status_and_the_request_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = McpClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="HTTP 500") as excinfo:
        client.list_tools(_server("https://mcp.test/rpc?token=query-secret"))

    message = str(excinfo.value)
    assert "query-secret" not in message
    assert SECRET_HEADER_VALUE not in message


def test_list_tools_sanitizes_an_unreachable_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "cannot connect to https://mcp.test/rpc?token=query-secret", request=request
        )

    client = McpClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="unreachable") as excinfo:
        client.list_tools(_server("https://mcp.test/rpc?token=query-secret"))

    assert "query-secret" not in str(excinfo.value)


def test_list_tools_sanitizes_a_malformed_server_url() -> None:
    client = McpClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    with pytest.raises(RuntimeError) as excinfo:
        client.list_tools(_server("mcp.test/rpc?token=query-secret"))

    message = str(excinfo.value)
    assert message.startswith("MCP server 'srv-1' is unreachable (")
    assert message.endswith(")")
    assert "query-secret" not in message
    assert "mcp.test" not in message


def test_list_tools_refuses_a_server_without_a_url() -> None:
    client = McpClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    with pytest.raises(RuntimeError, match="url"):
        client.list_tools(McpServer(id="srv-3", url="", headers={}, name="broken"))


def test_reprs_hide_the_secret_header_values_and_the_url_query() -> None:
    secret_url = "https://mcp.test/rpc?token=query-secret"

    assert SECRET_HEADER_VALUE not in repr(_server(secret_url))
    assert "query-secret" not in repr(_server(secret_url))

    secret = McpServerSecret(
        headers={"X-Api-Key": SECRET_HEADER_VALUE},
        url=secret_url,
        transport="streamable_http",
    )
    assert SECRET_HEADER_VALUE not in repr(secret)
    assert "query-secret" not in repr(secret)


def test_load_server_secrets_reads_headers_and_overrides() -> None:
    environ = {
        "MCP_SERVER_HEADERS": json.dumps(
            {
                "project_db": {
                    "X-Api-Key": SECRET_HEADER_VALUE,
                    "url": "https://mcp.test/rpc?token=abc",
                    "transport": "streamable_http",
                }
            }
        )
    }

    secrets = load_server_secrets(environ)

    assert sorted(secrets) == ["project_db"]
    assert secrets["project_db"] == McpServerSecret(
        headers={"X-Api-Key": SECRET_HEADER_VALUE},
        url="https://mcp.test/rpc?token=abc",
        transport="streamable_http",
    )
    assert SECRET_HEADER_VALUE not in repr(secrets["project_db"])


def test_load_server_secrets_is_empty_without_the_variable() -> None:
    assert load_server_secrets({}) == {}


def test_load_server_secrets_rejects_malformed_json_without_echoing_values() -> None:
    broken = '{"project_db": {"X-Api-Key": "' + SECRET_HEADER_VALUE + '"'

    with pytest.raises(ValueError) as excinfo:
        load_server_secrets({"MCP_SERVER_HEADERS": broken})

    assert SECRET_HEADER_VALUE not in str(excinfo.value)


def test_load_server_secrets_rejects_a_non_object_entry() -> None:
    with pytest.raises(ValueError, match="project_db"):
        load_server_secrets({"MCP_SERVER_HEADERS": json.dumps({"project_db": "not-an-object"})})


def test_load_server_secrets_rejects_a_non_string_header_value() -> None:
    with pytest.raises(ValueError, match="X-Api-Key"):
        load_server_secrets({"MCP_SERVER_HEADERS": json.dumps({"project_db": {"X-Api-Key": 7}})})
