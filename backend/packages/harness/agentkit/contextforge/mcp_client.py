"""Raw ContextForge MCP client: tools/list pagination and tools/call.

This module speaks MCP directly through the official SDK. The
langchain-mcp-adapters loader is deliberately not used here: it converts
inputSchema into a Pydantic model, and the round-trip can rewrite $ref/anyOf/
default. The sync task stores the schema exactly as ContextForge sent it, and
tool_execute calls the tool by its original (unprefixed) name.

Each operation opens one short MCP session, initializes it, performs the single
request and closes it, matching the design: ContextForge is the execution
endpoint, not a long-lived multiplexed connection.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from mcp import ClientSession, types as mcp_types
from mcp.client.streamable_http import streamablehttp_client

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.models import ContextForgeProtocolError, ContextForgeUnavailable


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
class McpSessionRequest:
    """Everything the transport needs to open one short MCP session."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 15.0
    read_timeout: float = 15.0
    verify_tls: bool = True


SessionFactory = Callable[[McpSessionRequest], AbstractAsyncContextManager[McpSession]]
"""Callable returning an async context manager that yields an MCP session."""


@asynccontextmanager
async def open_mcp_session(request: McpSessionRequest) -> AsyncIterator[ClientSession]:
    """Open one streamable-HTTP MCP session; the caller initializes it."""
    async with streamablehttp_client(
        request.url,
        headers=dict(request.headers),
        timeout=request.timeout,
        sse_read_timeout=max(request.timeout, request.read_timeout),
        **_transport_options(request.verify_tls),
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=datetime.timedelta(seconds=request.read_timeout),
        ) as session:
            yield session


def _transport_options(verify_tls: bool) -> dict[str, Any]:
    """Honour contextforge.verify_tls without reaching into the SDK internals.

    Verifying TLS is exactly what the SDK's own default httpx factory does, so
    the httpx_client_factory argument is omitted in that case and the
    transport default applies; only verify_tls=false installs a factory here.
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

    The signature matches the callable the streamable-HTTP transport expects
    (it passes its own headers, timeout and auth through), so this stands in
    for the SDK's create_mcp_http_client with verification switched off.
    """
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        verify=False,
        follow_redirects=False,
    )


class ContextForgeMcpClient:
    """The only MCP entry point to ContextForge: full tools/list, single tools/call."""

    def __init__(
        self,
        config: ContextForgeConfig,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._config = config
        self._session_factory: SessionFactory = session_factory or open_mcp_session

    @property
    def mcp_url(self) -> str:
        return self._config.resolved_mcp_url()

    def _configured(self) -> bool:
        return self._config.enabled and bool(self.mcp_url)

    def _headers(self) -> dict[str, str]:
        token = self._config.resolved_agent_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(self, read_timeout: float | None) -> McpSessionRequest:
        timeout = self._config.request_timeout_seconds
        return McpSessionRequest(
            url=self.mcp_url,
            headers=self._headers(),
            timeout=timeout,
            read_timeout=timeout if read_timeout is None else read_timeout,
            verify_tls=self._config.verify_tls,
        )

    @asynccontextmanager
    async def _session(self, read_timeout: float | None) -> AsyncIterator[McpSession]:
        async with self._session_factory(self._request(read_timeout)) as session:
            await session.initialize()
            yield session

    async def list_tools(self) -> list[mcp_types.Tool]:
        """Return every tool from ContextForge, following tools/list pagination."""
        if not self._configured():
            return []
        tools: list[mcp_types.Tool] = []
        seen: set[str] = set()
        async with self._session(None) as session:
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    return tools
                if cursor in seen:
                    raise ContextForgeProtocolError(
                        "ContextForge MCP tools/list repeated a pagination cursor"
                    )
                seen.add(cursor)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> mcp_types.CallToolResult:
        """Call one tool by its ContextForge (original, unprefixed) name."""
        if not self._configured():
            raise ContextForgeUnavailable("ContextForge MCP endpoint is not configured")
        read_timeout = None if timeout is None else datetime.timedelta(seconds=timeout)
        async with self._session(timeout) as session:
            return await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=read_timeout,
            )

    async def aclose(self) -> None:
        """Release lifecycle state; each request owns its short-lived session.

        There is no pooled client to close today, but the runtime owns this
        client and closes it with the Admin client so a future transport that
        does hold a connection has a defined shutdown hook.
        """


__all__ = [
    "ContextForgeMcpClient",
    "McpSession",
    "McpSessionRequest",
    "SessionFactory",
    "open_mcp_session",
]
