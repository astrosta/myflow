"""Error types shared by the per-server MCP client and its callers.

Every failure crossing the client boundary is one of these classes, so the sync
service, the runtime and the HTTP layer can map a failure to a status code
without inspecting SDK internals.  Messages are deliberately built from fixed
text plus a server id or exception *class*: request headers, tokens and full
URLs with query strings must never reach a log or an API response.
"""

from __future__ import annotations

from typing import Any


class McpServerError(RuntimeError):
    """Base error for one managed MCP server."""

    code = "mcp_server_error"
    status_code = 502


class McpServerNotFound(McpServerError):
    """No server with this id belongs to the caller.

    A server is user-private, so another owner's id is deliberately reported as
    "not found" rather than "forbidden": the response must not reveal that the row
    exists for somebody else.
    """

    code = "mcp_server_not_found"
    status_code = 404


class McpServerConflict(McpServerError):
    """A write conflicts with an existing server or the per-user limit."""

    code = "mcp_server_conflict"
    status_code = 409


class McpServerValidationError(McpServerError, ValueError):
    """The submitted server configuration is invalid (URL, headers, transport)."""

    code = "mcp_server_invalid"
    status_code = 422


class McpServerUnavailable(McpServerError):
    """DNS, TCP, TLS or HTTP-layer failure: the server could not be reached."""

    code = "mcp_server_unavailable"
    status_code = 502


class McpServerTimeout(McpServerError):
    """Connecting, initializing or waiting for a response exceeded its budget."""

    code = "mcp_server_timeout"
    status_code = 504


class McpProtocolError(McpServerError):
    """The peer answered, but not with a valid MCP initialize/list response."""

    code = "mcp_protocol_error"
    status_code = 502


class McpToolError(McpServerError):
    """The upstream server reported that the tool call itself failed.

    'result' keeps the MCP CallToolResult when the server signalled the failure
    through isError instead of a JSON-RPC error, so a caller can surface the
    tool's own error text without making a second request.
    """

    code = "mcp_tool_error"
    status_code = 502

    def __init__(self, message: str, *, result: Any = None) -> None:
        super().__init__(message)
        self.result = result


__all__ = [
    "McpProtocolError",
    "McpServerConflict",
    "McpServerError",
    "McpServerNotFound",
    "McpServerValidationError",
    "McpServerTimeout",
    "McpServerUnavailable",
    "McpToolError",
]
