"""MCP JSON-RPC client for reading a server's real tool schemas.

This module is deliberately a black-box HTTP client: it imports neither
backend.app nor agentkit. The Gateway only exposes tool summaries (no
inputSchema), so the dataset builder calls each server's MCP tools/list
directly, over the streamable-HTTP transport with a short
initialize / notifications/initialized handshake.

Server auth headers are secret. They come from the MCP_SERVER_HEADERS JSON map,
are excluded from every dataclass repr, and are never echoed in an error: the
server URL may carry a token in its query string, so transport failures are
reported by server id and exception type only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Protocol revision sent on initialize; servers negotiate down if needed.
_MCP_PROTOCOL_VERSION = "2025-06-18"

_JSONRPC_VERSION = "2.0"
_CLIENT_INFO = {"name": "myflow-evals", "version": "0.1.0"}
_ACCEPT = "application/json, text/event-stream"
_SESSION_HEADER = "Mcp-Session-Id"
_HEADERS_ENV = "MCP_SERVER_HEADERS"
_RESERVED_SECRET_KEYS = frozenset({"url", "transport"})
_DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class McpServer:
    """One MCP server of the fixed account, ready to be called.

    `name` is the store name: call_name is mcp__<name>__<tool>, exactly as
    agentkit builds it, and the list endpoint never returns that string.
    """

    id: str
    url: str
    headers: dict[str, str] = field(repr=False)
    name: str = ""


@dataclass(frozen=True)
class McpTool:
    """One tool as the server publishes it, including its real input schema."""

    call_name: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpServerSecret:
    """Per-server auth headers plus optional url/transport overrides.

    Header values are secrets: they are excluded from repr and never logged.
    """

    headers: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = None
    transport: str | None = None


def load_server_secrets(environ: Mapping[str, str] | None = None) -> dict[str, McpServerSecret]:
    """Read MCP_SERVER_HEADERS into per-server auth material, keyed by name or id.

    The accepted shape maps a server name or id to an object whose keys are
    header names and values are header values, with optional reserved "url" and
    "transport" overrides for servers whose list URL lost its query string::

        {"project_db": {"X-Api-Key": "...", "url": "https://mcp.test/rpc?token=..."}}

    An unset or empty variable means no server needs auth. Error messages name
    the offending server and key but never echo a header value.
    """
    env = os.environ if environ is None else environ
    raw = env.get(_HEADERS_ENV)
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid {_HEADERS_ENV}: {exc.msg} at position {exc.pos}"
        ) from None
    if not isinstance(data, Mapping):
        raise ValueError(
            f"invalid {_HEADERS_ENV}: expected an object mapping server name or id "
            "to that server's headers"
        )

    secrets: dict[str, McpServerSecret] = {}
    for key, entry in data.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"invalid {_HEADERS_ENV}: server keys must be non-empty strings"
            )
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"invalid {_HEADERS_ENV}: entry {key!r} must be an object of "
                "header names to values"
            )
        headers: dict[str, str] = {}
        url: str | None = None
        transport: str | None = None
        for name, value in entry.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"invalid {_HEADERS_ENV}: entry {key!r} has a non-string key"
                )
            if name in _RESERVED_SECRET_KEYS:
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        f"invalid {_HEADERS_ENV}: entry {key!r} has a non-string "
                        f"{name!r} override"
                    )
                if name == "url":
                    url = value
                else:
                    transport = value
                continue
            if not isinstance(value, str):
                raise ValueError(
                    f"invalid {_HEADERS_ENV}: entry {key!r} header {name!r} must be "
                    "a string value"
                )
            headers[name] = value
        secrets[key] = McpServerSecret(headers=headers, url=url, transport=transport)
    return secrets


class McpClient:
    """Call one MCP server's tools/list over streamable HTTP.

    A fresh client is created per list_tools call because each MCP server is its
    own origin with its own session: nothing from the account's Gateway Client
    (cookie jar, CSRF header) may travel to it.
    """

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    def list_tools(self, server: McpServer) -> list[McpTool]:
        """Return every tool the server publishes, following nextCursor."""
        if not server.url:
            raise RuntimeError(f"MCP server {server.id!r} has no url to call")
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            session_id = self._initialize(client, server)
            self._notify_initialized(client, server, session_id)
            return self._tools(client, server, session_id)

    def _initialize(self, client: httpx.Client, server: McpServer) -> str | None:
        request_id = 1
        response = self._post(
            client,
            server,
            {
                "jsonrpc": _JSONRPC_VERSION,
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": dict(_CLIENT_INFO),
                },
            },
            session_id=None,
        )
        self._result(response, server, request_id, "initialize")
        return response.headers.get(_SESSION_HEADER) or None

    def _notify_initialized(
        self, client: httpx.Client, server: McpServer, session_id: str | None
    ) -> None:
        response = self._post(
            client,
            server,
            {"jsonrpc": _JSONRPC_VERSION, "method": "notifications/initialized"},
            session_id=session_id,
        )
        self._check_status(response, server, "notifications/initialized")

    def _tools(
        self, client: httpx.Client, server: McpServer, session_id: str | None
    ) -> list[McpTool]:
        tools: list[McpTool] = []
        cursors: set[str] = set()
        cursor: str | None = None
        request_id = 2
        while True:
            response = self._post(
                client,
                server,
                {
                    "jsonrpc": _JSONRPC_VERSION,
                    "id": request_id,
                    "method": "tools/list",
                    "params": {"cursor": cursor} if cursor else {},
                },
                session_id=session_id,
            )
            result = self._result(response, server, request_id, "tools/list")
            published = result.get("tools")
            if isinstance(published, list):
                for raw in published:
                    tool = _tool(raw, server)
                    if tool is not None:
                        tools.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            if next_cursor in cursors:
                return tools
            cursors.add(next_cursor)
            cursor = next_cursor
            request_id += 1

    def _post(
        self,
        client: httpx.Client,
        server: McpServer,
        body: Mapping[str, Any],
        *,
        session_id: str | None,
    ) -> httpx.Response:
        headers = {"accept": _ACCEPT}
        if server.headers:
            headers.update(server.headers)
        if session_id:
            headers[_SESSION_HEADER] = session_id
        try:
            return client.post(server.url, json=dict(body), headers=headers)
        except httpx.HTTPError as exc:
            # The URL may carry a token in its query string; report the type only.
            raise RuntimeError(
                f"MCP server {server.id!r} is unreachable ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _check_status(response: httpx.Response, server: McpServer, what: str) -> None:
        if response.status_code >= 400:
            raise RuntimeError(
                f"MCP server {server.id!r} rejected {what} with HTTP {response.status_code}"
            )

    def _result(
        self,
        response: httpx.Response,
        server: McpServer,
        request_id: int,
        what: str,
    ) -> dict[str, Any]:
        self._check_status(response, server, what)
        for message in _jsonrpc_messages(response, server, what):
            if str(message.get("id")) != str(request_id):
                continue
            error = message.get("error")
            if isinstance(error, Mapping):
                raise RuntimeError(
                    f"MCP server {server.id!r} returned an error for {what}: "
                    f"{_error_text(error)}"
                )
            result = message.get("result")
            if isinstance(result, Mapping):
                return dict(result)
            raise RuntimeError(
                f"MCP server {server.id!r} returned no result object for {what}"
            )
        raise RuntimeError(
            f"MCP server {server.id!r} returned no JSON-RPC response for {what}"
        )


def _jsonrpc_messages(
    response: httpx.Response, server: McpServer, what: str
) -> list[dict[str, Any]]:
    """Decode either a plain JSON body or the SSE frames a server may stream."""
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        payloads = [
            line[len("data:") :].strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
    else:
        payloads = [response.text]
    messages: list[dict[str, Any]] = []
    for payload in payloads:
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"MCP server {server.id!r} returned a non-JSON response for {what}"
            ) from None
        if isinstance(decoded, Mapping):
            messages.append(dict(decoded))
    return messages


def _error_text(error: Mapping[str, Any]) -> str:
    code = error.get("code")
    message = error.get("message")
    text = message if isinstance(message, str) and message else "no message"
    return f"{code} {text}".strip() if code is not None else text


def _tool(raw: object, server: McpServer) -> McpTool | None:
    """Build one tool, or None when the server published no usable name."""
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    description = raw.get("description")
    schema = raw.get("inputSchema")
    return McpTool(
        call_name=f"mcp__{server.name}__{name}",
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=dict(schema) if isinstance(schema, Mapping) else {},
    )
