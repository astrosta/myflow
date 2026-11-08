"""Per-server MCP client for Streamable HTTP and SSE transports.

Each operation opens one short MCP session, initializes it exactly once,
performs a single logical operation (a full paginated tools/list or one
tools/call) and closes the session.  There is no long-lived multiplexed
connection: a managed server is an execution endpoint, not a shared gateway.

The public methods translate every transport/protocol failure into one of the
four McpServerError subclasses.  Their messages are built from fixed text, the
server slug and an exception *class name* only: request headers, bearer tokens
and full URLs (including query strings) never appear in an error string.
"""

from __future__ import annotations

import datetime
import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio
import httpx
from mcp import ClientSession, McpError, types as mcp_types
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import ValidationError as PydanticValidationError

from agentkit.mcp.errors import (
    McpProtocolError,
    McpServerError,
    McpServerTimeout,
    McpServerUnavailable,
    McpToolError,
)
from agentkit.mcp.models import MCP_TRANSPORTS, McpTransport


class McpSession(Protocol):
    """The slice of mcp.ClientSession this client uses, so tests can fake it."""

    async def initialize(self) -> mcp_types.InitializeResult: ...

    async def list_tools(self, cursor: str | None = None) -> mcp_types.ListToolsResult: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: datetime.timedelta | None = None,
    ) -> mcp_types.CallToolResult: ...


@dataclass(frozen=True, slots=True)
class McpServerConnection:
    """One stored MCP server, detached from the database row that produced it.

    'headers' is the already-decrypted plaintext mapping: decryption belongs to
    the caller (the runtime owns the cipher), and the client never logs it.
    """

    server_id: str
    url: str
    transport: McpTransport = "streamable_http"
    headers: Mapping[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    request_timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class McpSessionRequest:
    """Everything the transport needs to open one short MCP session.

    'timeout' is the connect/HTTP budget; 'read_timeout' is how long an
    initialize/tools/list/tools/call response may take.  The names match the
    old ContextForge client so the transport wiring stays familiar.
    """

    url: str
    transport: McpTransport = "streamable_http"
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 5.0
    read_timeout: float = 30.0
    verify_tls: bool = True
    server_id: str = ""


SessionFactory = Callable[[McpSessionRequest], AbstractAsyncContextManager[McpSession]]
"""Callable returning an async context manager that yields an MCP session."""


def _server_label(server_id: str) -> str:
    """A stable, non-secret label for error messages."""
    return f"MCP server {server_id!r}" if server_id else "MCP server"


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    """Flatten the ExceptionGroups anyio task groups raise around transport errors."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for child in exc.exceptions:
            leaves.extend(_exception_leaves(child))
        return leaves
    return [exc]


def _is_timeout(leaf: BaseException) -> bool:
    if isinstance(leaf, (TimeoutError, httpx.TimeoutException)):
        return True
    return isinstance(leaf, McpError) and leaf.error.code == 408


def _is_unavailable(leaf: BaseException) -> bool:
    """DNS/TCP/TLS/HTTP-layer failures never reach the MCP protocol."""
    return isinstance(
        leaf,
        (
            httpx.HTTPError,
            ssl.SSLError,
            OSError,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
        ),
    )


def _classify(
    exc: BaseException,
    *,
    server_id: str,
    stage: str,
) -> McpServerError:
    """Map an SDK/httpx failure to the client's public error vocabulary.

    The original exception is chained by the caller for logs, but its text is
    deliberately not copied into the returned message: SDK errors can include
    URLs and, in a proxy error body, credentials.
    """
    leaves = _exception_leaves(exc)
    label = _server_label(server_id)

    if any(_is_timeout(leaf) for leaf in leaves):
        return McpServerTimeout(f"{label} timed out during {stage}")

    if any(_is_unavailable(leaf) for leaf in leaves):
        kind = next(type(leaf).__name__ for leaf in leaves if _is_unavailable(leaf))
        return McpServerUnavailable(f"{label} is unavailable ({kind})")

    mcperror = next((leaf for leaf in leaves if isinstance(leaf, McpError)), None)
    if mcperror is not None:
        code = mcperror.error.code
        if stage == "tools/call":
            return McpToolError(f"{label} rejected the tool call (JSON-RPC error {code})")
        return McpProtocolError(f"{label} returned a JSON-RPC error during {stage} (code {code})")

    if any(isinstance(leaf, PydanticValidationError) for leaf in leaves):
        return McpProtocolError(f"{label} returned a malformed {stage} response")

    kind = type(leaves[0]).__name__ if leaves else type(exc).__name__
    if stage == "tools/call":
        return McpToolError(f"{label} failed the tool call ({kind})")
    return McpProtocolError(f"{label} failed during {stage} ({kind})")


def _transport_options(verify_tls: bool) -> dict[str, Any]:
    """Honour verify_tls without reaching into private SDK modules.

    Verifying TLS is exactly what the SDK's own default httpx factory does, so
    the factory argument is omitted in that case; only verify_tls=false
    installs a local factory.
    """
    if verify_tls:
        return {}
    return {"httpx_client_factory": _insecure_http_client_factory}


def _insecure_http_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """httpx.AsyncClient factory that skips TLS verification.

    The signature matches the callable both MCP transports expect, so this
    stands in for the SDK's create_mcp_http_client with verification off.
    """
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        verify=False,
        follow_redirects=False,
    )


@asynccontextmanager
async def open_mcp_session(request: McpSessionRequest) -> AsyncIterator[ClientSession]:
    """Open one MCP session on the transport named by request.transport.

    Streamable HTTP yields (read, write, get_session_id); legacy SSE yields
    (read, write).  The caller initializes the yielded session exactly once.
    """
    if request.transport not in MCP_TRANSPORTS:
        raise McpServerUnavailable(
            f"MCP server uses unsupported transport {request.transport!r}"
        )

    options: dict[str, Any] = {
        "headers": dict(request.headers),
        "timeout": request.timeout,
        "sse_read_timeout": request.read_timeout,
        **_transport_options(request.verify_tls),
    }
    read_timeout = datetime.timedelta(seconds=request.read_timeout)

    try:
        if request.transport == "streamable_http":
            async with streamablehttp_client(request.url, **options) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=read_timeout,
                ) as session:
                    yield session
            return

        async with sse_client(request.url, **options) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=read_timeout,
            ) as session:
                yield session
    except McpServerError:
        raise
    except Exception as exc:
        raise _classify(
            exc,
            server_id=request.server_id,
            stage="initialize",
        ) from exc


class McpServerClient:
    """Full tools/list plus one tools/call per short MCP session."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory: SessionFactory = session_factory or open_mcp_session

    def _request(
        self,
        connection: McpServerConnection,
        read_timeout: float | None = None,
    ) -> McpSessionRequest:
        return McpSessionRequest(
            url=connection.url,
            transport=connection.transport,
            headers=connection.headers,
            timeout=connection.connect_timeout_seconds,
            read_timeout=(
                connection.request_timeout_seconds
                if read_timeout is None
                else read_timeout
            ),
            verify_tls=connection.verify_tls,
            server_id=connection.server_id,
        )

    @asynccontextmanager
    async def _session(
        self,
        connection: McpServerConnection,
        read_timeout: float | None = None,
    ) -> AsyncIterator[McpSession]:
        """Open one transport session and initialize it exactly once.

        An AsyncExitStack keeps the transport's own context manager alive while
        the operation body runs, so each operation owns one short session.
        """
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(
                self._session_factory(self._request(connection, read_timeout))
            )
        except McpServerError:
            raise
        except Exception as exc:
            raise _classify(
                exc,
                server_id=connection.server_id,
                stage="initialize",
            ) from exc

        try:
            try:
                initialized = await session.initialize()
                # A real ClientSession always returns InitializeResult; None is
                # tolerated only because minimal test doubles return it.
                if initialized is not None and not isinstance(
                    initialized, mcp_types.InitializeResult
                ):
                    raise McpProtocolError(
                        f"{_server_label(connection.server_id)} returned a "
                        "malformed initialize response"
                    )
            except McpServerError:
                raise
            except Exception as exc:
                raise _classify(
                    exc,
                    server_id=connection.server_id,
                    stage="initialize",
                ) from exc
            yield session
        finally:
            await stack.aclose()

    def _validate_page(
        self,
        page: Any,
        *,
        server_id: str,
    ) -> mcp_types.ListToolsResult:
        label = _server_label(server_id)
        if not isinstance(page, mcp_types.ListToolsResult):
            raise McpProtocolError(f"{label} returned a malformed tools/list page")
        if not isinstance(page.tools, list) or not all(
            isinstance(tool, mcp_types.Tool) for tool in page.tools
        ):
            raise McpProtocolError(f"{label} returned malformed tools/list entries")
        if page.nextCursor is not None and not isinstance(page.nextCursor, str):
            raise McpProtocolError(f"{label} returned a malformed tools/list cursor")
        return page

    async def list_tools(self, connection: McpServerConnection) -> list[mcp_types.Tool]:
        """Return every tool from one server, following tools/list pagination."""
        try:
            async with self._session(connection) as session:
                tools: list[mcp_types.Tool] = []
                seen: set[str] = set()
                cursor: str | None = None
                while True:
                    page = self._validate_page(
                        await session.list_tools(cursor=cursor),
                        server_id=connection.server_id,
                    )
                    tools.extend(page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        return tools
                    if cursor in seen:
                        raise McpProtocolError(
                            f"{_server_label(connection.server_id)} repeated a "
                            "tools/list pagination cursor"
                        )
                    seen.add(cursor)
        except McpServerError:
            raise
        except Exception as exc:
            raise _classify(
                exc,
                server_id=connection.server_id,
                stage="tools/list",
            ) from exc

    async def call_tool(
        self,
        connection: McpServerConnection,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> mcp_types.CallToolResult:
        """Call one tool by its upstream (unprefixed) name."""
        read_timeout = (
            connection.request_timeout_seconds if timeout is None else timeout
        )
        try:
            async with self._session(connection, read_timeout) as session:
                result = await session.call_tool(
                    name,
                    arguments,
                    read_timeout_seconds=datetime.timedelta(seconds=read_timeout),
                )
                if not isinstance(result, mcp_types.CallToolResult):
                    raise McpProtocolError(
                        f"{_server_label(connection.server_id)} returned a "
                        "malformed tools/call response"
                    )
                if result.isError:
                    raise McpToolError(
                        f"{_server_label(connection.server_id)} reported that tool "
                        f"{name!r} failed",
                        result=result,
                    )
                return result
        except McpServerError:
            raise
        except Exception as exc:
            raise _classify(
                exc,
                server_id=connection.server_id,
                stage="tools/call",
            ) from exc


__all__ = [
    "McpProtocolError",
    "McpServerClient",
    "McpServerConnection",
    "McpServerError",
    "McpServerTimeout",
    "McpServerUnavailable",
    "McpSession",
    "McpSessionRequest",
    "McpToolError",
    "SessionFactory",
    "open_mcp_session",
]
