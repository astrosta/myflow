"""Error types shared by the per-server MCP client and its callers.

Every failure crossing the client boundary is one of these classes, so the sync
service, the runtime and the HTTP layer can map a failure to a status code
without inspecting SDK internals.  Messages are deliberately built from fixed
text plus a server id or exception *class*: request headers, tokens and full
URLs with query strings must never reach a log or an API response.

The sanitizer at the bottom of this module is the one place that turns an
arbitrary exception into text that may be stored (mcp_servers.last_error) or
logged.  Both the management service and the discovery sync use it: a second,
weaker copy is how a URL with a query token survives in a row shown in the UI.
"""

from __future__ import annotations

import re
from typing import Any

from agentkit.mcp.models import MAX_ERROR_CHARS, truncate_error


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


class McpServerConfigurationError(McpServerError):
    """The stored server row cannot be used (for example, headers do not decrypt).

    This is a deployment/data problem, not a bad request: the API must answer a
    sanitized 500 rather than telling the caller their input was invalid.
    """

    code = "mcp_server_configuration_error"
    status_code = 500


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


#: The stored fallback when an exception carries nothing worth keeping.
DEFAULT_ERROR_TEXT = "MCP server sync failed"

#: Stored and displayed text is scrubbed before either happens: these patterns cover
#: the three ways a credential or an unpublished endpoint gets into a message.
_ENDPOINT = re.compile(r"(?i)[a-z][a-z0-9+.\-]*://[^\s]+")
_CREDENTIAL_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
    r"api-key|x-auth-token|x-access-token)\b\s*[:=][^\r\n]*"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key)\s*[:=]\s*\S+"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_error_text(text: str | None, *, max_chars: int = MAX_ERROR_CHARS) -> str:
    """Scrub a message of endpoints, credentials and control characters.

    Order matters: the header, bearer and secret-field patterns run first, so the
    endpoint pattern (which swallows everything up to whitespace, query string
    included) is not the only thing standing between a token and the database.
    """
    cleaned = _CREDENTIAL_HEADER.sub(r"\1: ****", str(text or ""))
    cleaned = _BEARER.sub("Bearer ****", cleaned)
    cleaned = _SECRET_FIELD.sub(r"\1=****", cleaned)
    cleaned = _ENDPOINT.sub("[redacted endpoint]", cleaned)
    cleaned = _CONTROL.sub(" ", cleaned).strip()
    return cleaned[: max(1, int(max_chars))]


def sanitize_error_message(
    exc: BaseException,
    *,
    label: str = "",
    fallback: str = DEFAULT_ERROR_TEXT,
) -> str:
    """One stored/loggable failure message that cannot contain a credential.

    Client errors (McpServerError) are already built from fixed text plus the
    server id, so their text is the useful part and is kept.  Any other exception is
    reduced to its class name, because its text is not ours to trust: an SDK, proxy
    or database error body can carry the URL and the auth header with it.

    label is prefixed when supplied, which is how the sync names the server
    ("MCP server 'anysearch': ..."); the result is then scrubbed by
    sanitize_error_text and bounded to the last_error column.
    """
    if isinstance(exc, McpServerError):
        body = str(exc)
    elif label:
        body = "sync failed ({0})".format(type(exc).__name__)
    else:
        body = "{0} ({1})".format(DEFAULT_ERROR_TEXT, type(exc).__name__)
    text = "{0}: {1}".format(label, body) if label else body
    return truncate_error(sanitize_error_text(text)) or fallback


__all__ = [
    "DEFAULT_ERROR_TEXT",
    "McpProtocolError",
    "McpServerConfigurationError",
    "McpServerConflict",
    "McpServerError",
    "McpServerNotFound",
    "McpServerValidationError",
    "McpServerTimeout",
    "McpServerUnavailable",
    "McpToolError",
    "sanitize_error_message",
    "sanitize_error_text",
]
