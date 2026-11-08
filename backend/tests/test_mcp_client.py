"""Per-server MCP client: transports, pagination, calls and error mapping.

The client speaks MCP directly through the official SDK, opening one short
session per operation. These tests fake the session/transport boundary, so no
MCP server is needed, and pin the contract the sync and execution tasks use:
the selected transport, the caller's headers, every tools/list page, tools/call
by the upstream name, and safe error classification.
"""

from __future__ import annotations

import datetime
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from agentkit.mcp import client as mcp_client
from agentkit.mcp.client import (
    McpProtocolError,
    McpServerClient,
    McpServerConnection,
    McpServerTimeout,
    McpServerUnavailable,
    McpSessionRequest,
    McpToolError,
)


def connection(**overrides: Any) -> McpServerConnection:
    base: dict[str, Any] = {
        "server_id": "srv-1",
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable_http",
        "headers": {"Authorization": "Bearer top-secret"},
        "verify_tls": True,
        "request_timeout_seconds": 30.0,
        "connect_timeout_seconds": 5.0,
    }
    return McpServerConnection(**{**base, **overrides})


class FakeSession:
    """Stands in for mcp.ClientSession; records everything the client does."""

    def __init__(
        self,
        *,
        pages: list[mcp_types.ListToolsResult] | None = None,
        call_result: mcp_types.CallToolResult | None = None,
        initialize_error: BaseException | None = None,
        list_error: BaseException | None = None,
        call_error: BaseException | None = None,
        initialize_result: Any = "valid",
        list_result: Any = "pages",
    ) -> None:
        self._pages = list(pages or [])
        self._call_result = call_result or _call_result()
        self._initialize_error = initialize_error
        self._list_error = list_error
        self._call_error = call_error
        self._initialize_result = initialize_result
        self._list_result = list_result
        self.initialized = 0
        self.cursors: list[str | None] = []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.call_timeouts: list[datetime.timedelta | None] = []

    async def initialize(self) -> mcp_types.InitializeResult:
        self.initialized += 1
        if self._initialize_error is not None:
            raise self._initialize_error
        if self._initialize_result == "valid":
            return mcp_types.InitializeResult(
                protocolVersion="2025-06-18",
                capabilities=mcp_types.ServerCapabilities(),
                serverInfo=mcp_types.Implementation(name="fake", version="1.0"),
            )
        return self._initialize_result

    async def list_tools(self, cursor: str | None = None) -> mcp_types.ListToolsResult:
        if self._list_error is not None:
            raise self._list_error
        self.cursors.append(cursor)
        if self._list_result != "pages":
            return self._list_result
        if len(self.cursors) > len(self._pages):
            return mcp_types.ListToolsResult(tools=[])
        return self._pages[len(self.cursors) - 1]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: datetime.timedelta | None = None,
    ) -> mcp_types.CallToolResult:
        if self._call_error is not None:
            raise self._call_error
        self.calls.append((name, arguments))
        self.call_timeouts.append(read_timeout_seconds)
        return self._call_result


class FakeTransport:
    """Session factory double: records each request, yields the fake session."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session
        self.transports: list[str] = []
        self.requests: list[McpSessionRequest] = []

    @asynccontextmanager
    async def factory(self, request: McpSessionRequest) -> AsyncIterator[FakeSession]:
        # Mirrors open_mcp_session's selection so the contract is readable here.
        self.transports.append(
            "streamablehttp_client"
            if request.transport == "streamable_http"
            else "sse_client"
        )
        self.requests.append(request)
        yield self._session


def _tool(name: str, schema: dict[str, Any] | None = None) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=f"{name} description",
        inputSchema=schema or {"type": "object"},
    )


def _page(
    *tools: mcp_types.Tool, next_cursor: str | None = None
) -> mcp_types.ListToolsResult:
    return mcp_types.ListToolsResult(tools=list(tools), nextCursor=next_cursor)


def _call_result(*, is_error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
        isError=is_error,
    )


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession(pages=[_page()])


@pytest.fixture
def fake_transport(fake_session: FakeSession) -> FakeTransport:
    return FakeTransport(fake_session)


@pytest.fixture
def client(fake_transport: FakeTransport) -> McpServerClient:
    return McpServerClient(session_factory=fake_transport.factory)


@pytest.mark.asyncio
async def test_streamable_and_sse_select_the_right_transport(
    fake_transport: FakeTransport,
) -> None:
    client = McpServerClient(session_factory=fake_transport.factory)

    await client.list_tools(connection(transport="streamable_http"))
    await client.list_tools(connection(transport="sse"))

    assert fake_transport.transports == ["streamablehttp_client", "sse_client"]

@pytest.mark.asyncio
async def test_list_tools_follows_cursor_and_call_uses_original_name(
    fake_session: FakeSession, fake_transport: FakeTransport
) -> None:
    fake_session._pages = [
        _page(_tool("search"), next_cursor="page-2"),
        _page(_tool("fetch")),
    ]
    client = McpServerClient(session_factory=fake_transport.factory)

    tools = await client.list_tools(connection())
    result = await client.call_tool(connection(), "search", {"q": "x"})

    assert [tool.name for tool in tools] == ["search", "fetch"]
    assert fake_session.cursors == [None, "page-2"]
    assert fake_session.calls == [("search", {"q": "x"})]
    assert result.content
    assert fake_session.initialized == 2, "one initialize per short session"


@pytest.mark.asyncio
async def test_list_tools_rejects_a_repeated_cursor(fake_transport: FakeTransport) -> None:
    fake_transport._session._pages = [
        _page(_tool("search"), next_cursor="page-2"),
        _page(_tool("fetch"), next_cursor="page-2"),
    ]
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError) as raised:
        await client.list_tools(connection())

    assert "top-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_call_tool_uses_default_and_explicit_read_timeouts(
    fake_transport: FakeTransport,
) -> None:
    client = McpServerClient(session_factory=fake_transport.factory)

    await client.call_tool(connection(), "search", {"q": "x"})
    await client.call_tool(connection(), "search", {"q": "y"}, timeout=2.5)

    assert fake_transport._session.call_timeouts == [
        datetime.timedelta(seconds=30.0),
        datetime.timedelta(seconds=2.5),
    ]
    assert [request.read_timeout for request in fake_transport.requests] == [30.0, 2.5]
    assert {request.timeout for request in fake_transport.requests} == {5.0}


@pytest.mark.asyncio
async def test_request_carries_headers_tls_and_the_operation_budget(
    fake_transport: FakeTransport,
) -> None:
    client = McpServerClient(session_factory=fake_transport.factory)

    await client.list_tools(
        connection(
            headers={"Authorization": "Bearer other-secret", "X-Tenant": "acme"},
            verify_tls=False,
            request_timeout_seconds=12.5,
            connect_timeout_seconds=3.5,
        )
    )

    request = fake_transport.requests[0]
    assert request.url == "https://mcp.example.com/mcp"
    assert request.transport == "streamable_http"
    assert request.headers == {
        "Authorization": "Bearer other-secret",
        "X-Tenant": "acme",
    }
    assert request.verify_tls is False
    assert request.timeout == 3.5
    assert request.read_timeout == 12.5


# --- transport wiring: SDK functions, headers, TLS factory -------------------


class _RecordedTransport:
    """Replacement for streamablehttp_client / sse_client."""

    def __init__(
        self,
        *,
        tuple_size: int,
        error: BaseException | None = None,
    ) -> None:
        self.tuple_size = tuple_size
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def __call__(self, url: str, **options: Any) -> AsyncIterator[tuple[Any, ...]]:
        self.calls.append((url, options))
        if self.error is not None:
            raise self.error
        read_send, read_stream = anyio.create_memory_object_stream[Any](0)
        write_stream, write_receive = anyio.create_memory_object_stream[Any](0)
        async with read_send, write_receive:
            if self.tuple_size == 3:
                yield read_stream, write_stream, lambda: None
            else:
                yield read_stream, write_stream


def _verify_mode(client: httpx.AsyncClient) -> ssl.VerifyMode:
    """The verify mode of the SSL context the client will actually use."""
    transport = client._transport
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    return transport._pool._ssl_context.verify_mode


@pytest.mark.asyncio
async def test_streamable_http_passes_headers_and_uses_the_sdk_default_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport(tuple_size=3)
    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    request = McpSessionRequest(
        url="https://mcp.example.com/mcp",
        transport="streamable_http",
        headers={"Authorization": "Bearer top-secret"},
        timeout=5.0,
        read_timeout=12.5,
        verify_tls=True,
    )

    async with mcp_client.open_mcp_session(request) as session:
        assert isinstance(session, mcp_client.ClientSession)

    url, options = transport.calls[0]
    assert url == request.url
    assert options["headers"] == {"Authorization": "Bearer top-secret"}
    assert options["timeout"] == 5.0
    assert options["sse_read_timeout"] == 12.5
    assert "httpx_client_factory" not in options


@pytest.mark.asyncio
async def test_sse_passes_headers_and_the_local_tls_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport(tuple_size=2)
    monkeypatch.setattr(mcp_client, "sse_client", transport)
    request = McpSessionRequest(
        url="https://mcp.example.com/sse",
        transport="sse",
        headers={"Authorization": "Bearer top-secret"},
        timeout=5.0,
        read_timeout=12.5,
        verify_tls=False,
    )

    async with mcp_client.open_mcp_session(request) as session:
        assert isinstance(session, mcp_client.ClientSession)

    url, options = transport.calls[0]
    assert url == request.url
    assert options["headers"] == {"Authorization": "Bearer top-secret"}
    assert options["timeout"] == 5.0
    assert options["sse_read_timeout"] == 12.5
    assert options["httpx_client_factory"] is mcp_client._insecure_http_client_factory


@pytest.mark.asyncio
async def test_verify_tls_false_skips_verification_in_the_local_factory() -> None:
    factory = mcp_client._insecure_http_client_factory
    client = factory(
        headers={"Authorization": "Bearer top-secret"},
        timeout=httpx.Timeout(5.0, read=12.5),
        auth=None,
    )
    async with client:
        assert client.headers["authorization"] == "Bearer top-secret"
        assert client.timeout == httpx.Timeout(5.0, read=12.5)
        assert _verify_mode(client) is ssl.CERT_NONE
        assert client.follow_redirects is False


def test_local_factory_matches_the_sdk_call_contract() -> None:
    client = mcp_client._insecure_http_client_factory()
    assert client.timeout == httpx.Timeout(30.0, read=300.0)
    assert _verify_mode(client) is ssl.CERT_NONE


@pytest.mark.asyncio
async def test_unsupported_transport_fails_before_touching_the_network() -> None:
    request = McpSessionRequest(url="https://mcp.example.com/mcp", transport="stdio")

    with pytest.raises(McpServerUnavailable):
        async with mcp_client.open_mcp_session(request):
            pass


# --- error mapping: transport, timeout, protocol, tool -----------------------


@pytest.mark.asyncio
async def test_initialize_timeout_is_reported_as_a_timeout(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_error = httpx.ReadTimeout("slow")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerTimeout) as raised:
        await client.list_tools(connection())

    assert "top-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_list_timeout_is_reported_as_a_timeout(fake_transport: FakeTransport) -> None:
    fake_transport._session._list_error = TimeoutError("slow")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerTimeout):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_sdk_timeout_error_is_reported_as_a_timeout(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._list_error = McpError(
        mcp_types.ErrorData(code=408, message="Timed out")
    )
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerTimeout):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_connection_failure_is_reported_as_unavailable(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_error = httpx.ConnectError(
        "Authorization: Bearer top-secret"
    )
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerUnavailable) as raised:
        await client.list_tools(connection(url="https://mcp.example.com/mcp?token=url-secret"))

    assert "top-secret" not in str(raised.value)
    assert "url-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_tls_failure_is_reported_as_unavailable(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._list_error = ssl.SSLError("certificate verify failed")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerUnavailable):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_malformed_initialize_is_a_protocol_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_error = ValueError("bad initialize result")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_malformed_initialize_result_is_a_protocol_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_result = object()
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_none_initialize_result_is_a_protocol_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_result = None
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_malformed_list_page_is_a_protocol_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._list_result = None
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError):
        await client.list_tools(connection())


@pytest.mark.asyncio
async def test_upstream_jsonrpc_call_error_is_a_tool_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._call_error = McpError(
        mcp_types.ErrorData(code=-32000, message="upstream exploded")
    )
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpToolError) as raised:
        await client.call_tool(connection(), "search", {"q": "x"})

    assert "top-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_unexpected_call_failure_is_a_tool_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._call_error = RuntimeError("upstream exploded")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpToolError):
        await client.call_tool(connection(), "search", {"q": "x"})


@pytest.mark.asyncio
async def test_is_error_result_is_returned_as_data(
    fake_transport: FakeTransport,
) -> None:
    result = _call_result(is_error=True)
    fake_transport._session._call_result = result
    client = McpServerClient(session_factory=fake_transport.factory)

    returned = await client.call_tool(connection(), "search", {"q": "x"})

    assert returned is result
    assert returned.isError is True
    assert returned.content


@pytest.mark.asyncio
async def test_call_timeout_is_reported_as_a_timeout(fake_transport: FakeTransport) -> None:
    fake_transport._session._call_error = httpx.WriteTimeout("slow")
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerTimeout):
        await client.call_tool(connection(), "search", {"q": "x"})


@pytest.mark.asyncio
async def test_malformed_call_result_is_a_protocol_error(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._call_result = object()
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpProtocolError):
        await client.call_tool(connection(), "search", {"q": "x"})


@pytest.mark.asyncio
async def test_transport_open_timeout_is_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport(tuple_size=3, error=httpx.ReadTimeout("slow"))
    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    request = McpSessionRequest(url="https://mcp.example.com/mcp")

    with pytest.raises(McpServerTimeout):
        async with mcp_client.open_mcp_session(request):
            pass


@pytest.mark.asyncio
async def test_consumer_exception_from_open_session_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordedTransport(tuple_size=3)
    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    request = McpSessionRequest(
        url="https://mcp.example.com/mcp", server_id="srv-1"
    )

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with mcp_client.open_mcp_session(request):
            raise RuntimeError("consumer failed")


@pytest.mark.asyncio
async def test_exception_group_connection_failure_is_unavailable(
    fake_transport: FakeTransport,
) -> None:
    fake_transport._session._initialize_error = ExceptionGroup(
        "transport failed", [httpx.ConnectError("refused")]
    )
    client = McpServerClient(session_factory=fake_transport.factory)

    with pytest.raises(McpServerUnavailable):
        await client.list_tools(connection())
