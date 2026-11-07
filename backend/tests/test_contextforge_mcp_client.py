"""Raw ContextForge MCP client: pagination, verbatim schemas, call wiring.

The client is a thin wrapper over the official mcp SDK. These tests fake the
session so no ContextForge endpoint is needed, and they assert the contract the
sync and tool_execute tasks consume: every tools/list page, the inputSchema
exactly as ContextForge sent it, and tools/call addressed by the original name.
"""

from __future__ import annotations

import datetime
import inspect
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
import pytest
from mcp import types as mcp_types

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge import mcp_client
from agentkit.contextforge.mcp_client import ContextForgeMcpClient, McpSessionRequest
from agentkit.contextforge.models import ContextForgeProtocolError, ContextForgeUnavailable

# Verbatim-shape fixture: $defs/anyOf/default/$ref must survive untouched.
WEATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "default": "Tokyo"},
        "unit": {"$ref": "#/$defs/unit"},
    },
    "required": ["city"],
    "$defs": {"unit": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    "additionalProperties": False,
}
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _config(**overrides: Any) -> ContextForgeConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://cf.example.com",
        "admin_token": "admin-token",
        "agent_token": "agent-token",
        "tool_loading": "eager",
        "search": {"enabled": False},
    }
    return ContextForgeConfig(**{**base, **overrides})


class FakeSession:
    """Stands in for mcp.ClientSession; records everything the client does."""

    def __init__(
        self,
        *,
        pages: list[mcp_types.ListToolsResult] | None = None,
        call_result: mcp_types.CallToolResult | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self._call_result = call_result
        self.initialized = 0
        self.cursors: list[str | None] = []
        self.calls: list[tuple[str, dict[str, Any] | None, datetime.timedelta | None]] = []

    async def initialize(self) -> None:
        self.initialized += 1

    async def list_tools(self, cursor: str | None = None) -> mcp_types.ListToolsResult:
        self.cursors.append(cursor)
        assert len(self.cursors) <= len(self._pages), "tools/list paged past the fixture"
        return self._pages[len(self.cursors) - 1]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: datetime.timedelta | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, arguments, read_timeout_seconds))
        assert self._call_result is not None, "call_tool invoked without a fixture"
        return self._call_result


def _transport(
    session: FakeSession, requests: list[McpSessionRequest]
) -> Callable[[McpSessionRequest], Any]:
    """Session factory double: records the request, yields the fake session."""

    @asynccontextmanager
    async def factory(request: McpSessionRequest) -> AsyncIterator[FakeSession]:
        requests.append(request)
        yield session

    return factory


def _page(*tools: mcp_types.Tool, next_cursor: str | None = None) -> mcp_types.ListToolsResult:
    return mcp_types.ListToolsResult(tools=list(tools), nextCursor=next_cursor)


def _tool(name: str, schema: dict[str, Any]) -> mcp_types.Tool:
    return mcp_types.Tool(name=name, description=f"{name} description", inputSchema=schema)


def test_client_uses_the_raw_mcp_sdk_not_the_langchain_adapter() -> None:
    source = inspect.getsource(mcp_client)
    assert "langchain_mcp_adapters" not in source
    assert "streamablehttp_client" in source
    assert "ClientSession" in source


@pytest.mark.asyncio
async def test_list_tools_walks_every_cursor_page_and_keeps_schema_verbatim() -> None:
    session = FakeSession(
        pages=[
            _page(_tool("weather_lookup", WEATHER_SCHEMA), next_cursor="page-2"),
            _page(_tool("doc_search", SEARCH_SCHEMA)),
        ]
    )
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(_config(), session_factory=_transport(session, requests))

    tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["weather_lookup", "doc_search"]
    assert session.initialized == 1, "one short MCP session per tools/list"
    assert session.cursors == [None, "page-2"], "pagination must follow nextCursor"
    assert tools[0].inputSchema == WEATHER_SCHEMA
    assert tools[1].inputSchema == SEARCH_SCHEMA
    # Verbatim, not a Pydantic round-trip: nested keywords and defaults survive.
    assert tools[0].inputSchema["properties"]["unit"] == {"$ref": "#/$defs/unit"}
    assert tools[0].inputSchema["properties"]["city"]["default"] == "Tokyo"


@pytest.mark.asyncio
async def test_list_tools_uses_resolved_mcp_url_timeout_and_tls_setting() -> None:
    session = FakeSession(pages=[_page(_tool("weather_lookup", WEATHER_SCHEMA))])
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(
        _config(
            virtual_server_id="srv-1",
            request_timeout_seconds=7.5,
            verify_tls=False,
        ),
        session_factory=_transport(session, requests),
    )

    await client.list_tools()

    assert requests[0].url == "https://cf.example.com/servers/srv-1/mcp"
    assert requests[0].timeout == 7.5
    assert requests[0].read_timeout == 7.5
    assert requests[0].verify_tls is False


@pytest.mark.asyncio
async def test_authorization_header_comes_from_agent_token() -> None:
    session = FakeSession(pages=[_page()])
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(_config(), session_factory=_transport(session, requests))

    assert await client.list_tools() == []
    assert requests[0].headers == {"Authorization": "Bearer agent-token"}


@pytest.mark.asyncio
async def test_authorization_header_falls_back_to_admin_token() -> None:
    session = FakeSession(pages=[_page()])
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(
        _config(agent_token=""), session_factory=_transport(session, requests)
    )

    await client.list_tools()
    assert requests[0].headers == {"Authorization": "Bearer admin-token"}


@pytest.mark.asyncio
async def test_no_authorization_header_without_any_token() -> None:
    session = FakeSession(pages=[_page()])
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(
        _config(agent_token="", admin_token=""),
        session_factory=_transport(session, requests),
    )

    await client.list_tools()
    assert requests[0].headers == {}


@pytest.mark.asyncio
async def test_call_tool_passes_the_original_name_and_arguments() -> None:
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="sunny")], isError=False
    )
    session = FakeSession(call_result=result)
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(_config(), session_factory=_transport(session, requests))

    returned = await client.call_tool("weather_lookup", {"city": "Tokyo"})

    assert returned is result
    assert session.initialized == 1
    assert session.calls == [("weather_lookup", {"city": "Tokyo"}, None)]
    assert requests[0].read_timeout == 15.0, "default per-call budget is request_timeout"


@pytest.mark.asyncio
async def test_call_tool_honours_an_explicit_timeout() -> None:
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")], isError=False
    )
    session = FakeSession(call_result=result)
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(_config(), session_factory=_transport(session, requests))

    await client.call_tool("weather_lookup", {"city": "Tokyo"}, timeout=2.5)

    assert session.calls == [
        ("weather_lookup", {"city": "Tokyo"}, datetime.timedelta(seconds=2.5))
    ]
    assert requests[0].read_timeout == 2.5


@pytest.mark.asyncio
async def test_repeated_cursor_stops_instead_of_looping_forever() -> None:
    session = FakeSession(
        pages=[
            _page(_tool("weather_lookup", WEATHER_SCHEMA), next_cursor="page-2"),
            _page(_tool("doc_search", SEARCH_SCHEMA), next_cursor="page-2"),
        ]
    )
    requests: list[McpSessionRequest] = []
    client = ContextForgeMcpClient(_config(), session_factory=_transport(session, requests))

    with pytest.raises(ContextForgeProtocolError):
        await client.list_tools()


@pytest.mark.asyncio
async def test_disabled_client_makes_no_network_calls() -> None:
    def factory(request: McpSessionRequest) -> Any:
        raise AssertionError("a disabled client must not open an MCP session")

    client = ContextForgeMcpClient(_config(enabled=False), session_factory=factory)

    assert await client.list_tools() == []
    with pytest.raises(ContextForgeUnavailable):
        await client.call_tool("weather_lookup", {"city": "Tokyo"})


# --- transport wiring: the SDK's default httpx factory vs. a local one ---------


class _RecordedTransport:
    """Replacement for streamablehttp_client: records its options, yields streams."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def __call__(self, url: str, **options: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        self.calls.append((url, options))
        read_send, read_stream = anyio.create_memory_object_stream[Any](0)
        write_stream, write_receive = anyio.create_memory_object_stream[Any](0)
        async with read_send, write_receive:
            yield read_stream, write_stream, lambda: None


def _verify_mode(client: httpx.AsyncClient) -> ssl.VerifyMode:
    """The verify mode of the SSL context the client will actually use."""
    transport = client._transport
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    return transport._pool._ssl_context.verify_mode


def test_module_takes_nothing_private_from_the_mcp_sdk() -> None:
    """A private SDK module may move within mcp 1.x and break every import."""
    source = inspect.getsource(mcp_client)
    assert "mcp.shared" not in source
    assert "_httpx_utils" not in source


@pytest.mark.asyncio
async def test_verify_tls_true_leaves_the_http_client_to_the_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport()
    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    request = McpSessionRequest(
        url="https://cf.example.com/servers/srv-1/mcp",
        headers={"Authorization": "Bearer agent-token"},
        timeout=7.5,
        read_timeout=9.0,
        verify_tls=True,
    )

    async with mcp_client.open_mcp_session(request) as session:
        assert isinstance(session, mcp_client.ClientSession)

    assert len(transport.calls) == 1
    url, options = transport.calls[0]
    assert url == request.url
    assert options["headers"] == {"Authorization": "Bearer agent-token"}
    assert options["timeout"] == 7.5
    assert options["sse_read_timeout"] == 9.0
    assert "httpx_client_factory" not in options, "verify the SDK default is used"


@pytest.mark.asyncio
async def test_verify_tls_false_installs_the_local_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport()
    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    request = McpSessionRequest(url="https://cf.example.com/mcp", verify_tls=False)

    async with mcp_client.open_mcp_session(request):
        pass

    assert len(transport.calls) == 1
    _, options = transport.calls[0]
    assert options["httpx_client_factory"] is mcp_client._insecure_http_client_factory

    # The transport calls the factory with its own headers/timeout/auth.
    client = options["httpx_client_factory"](
        headers={"Authorization": "Bearer agent-token"},
        timeout=httpx.Timeout(7.5, read=9.0),
        auth=None,
    )
    async with client:
        assert client.headers["authorization"] == "Bearer agent-token"
        assert client.timeout == httpx.Timeout(7.5, read=9.0)
        assert _verify_mode(client) is ssl.CERT_NONE, "verify_tls=false must skip TLS"


def test_local_factory_matches_the_sdk_call_contract() -> None:
    """No arguments at all still yields the SDK-style timeout, with TLS off."""
    client = mcp_client._insecure_http_client_factory()
    assert client.timeout == httpx.Timeout(30.0, read=300.0)
    assert _verify_mode(client) is ssl.CERT_NONE
    assert client.follow_redirects is False

