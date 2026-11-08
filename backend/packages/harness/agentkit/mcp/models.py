"""Canonical MCP server vocabulary: names, transports, statuses, the stored row shape.

The values here are the contract the store, the sync service and the HTTP API all
share, so a bad name or an unknown transport fails in one place instead of leaking
into a call name the model cannot use.

* `name` is a stable slug (`[a-z0-9-]+`) and immutable after creation: it is baked
  into every derived `mcp__<server>__<tool>` call name, so renaming a server would
  silently invalidate every tool the model already retrieved;
* `display_name` is the user-facing label and may change freely;
* `status` is `pending` / `active` / `error` / `disabled` - a derived summary of the
  last sync, never a user-editable field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

McpTransport = Literal["streamable_http", "sse"]
McpServerStatus = Literal["pending", "active", "error", "disabled"]

#: v1 supports the two HTTP transports only; stdio is deliberately out of scope.
MCP_TRANSPORTS: tuple[str, ...] = ("streamable_http", "sse")
#: `disabled` means the user switched the server off; `pending` means it still owes us
#: a successful initialize + tools/list.
MCP_SERVER_STATUSES: tuple[str, ...] = ("pending", "active", "error", "disabled")

MAX_SERVER_NAME_CHARS = 128
#: `last_error` is shown in the UI; a runaway upstream traceback must not bloat the row.
MAX_ERROR_CHARS = 2000

SERVER_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")


def is_valid_server_name(name: str | None) -> bool:
    """True for a slug that is safe to bake into a call name."""
    text = str(name or "").strip()
    return (
        bool(text)
        and len(text) <= MAX_SERVER_NAME_CHARS
        and bool(SERVER_NAME_PATTERN.fullmatch(text))
    )


def validate_server_name(name: str | None) -> str:
    """Return the slug, or raise `ValueError` naming the rule it broke."""
    text = str(name or "").strip()
    if not text:
        raise ValueError("mcp_servers.name is required")
    if len(text) > MAX_SERVER_NAME_CHARS:
        raise ValueError(
            f"mcp_servers.name must be at most {MAX_SERVER_NAME_CHARS} characters"
        )
    if not SERVER_NAME_PATTERN.fullmatch(text):
        raise ValueError(
            "mcp_servers.name must match [a-z0-9-]+: it is immutable and becomes part "
            "of every derived tool call name"
        )
    return text


def validate_transport(transport: str | None) -> str:
    """Return the transport, or raise `ValueError` listing the supported ones."""
    text = str(transport or "").strip()
    if text not in MCP_TRANSPORTS:
        raise ValueError(
            f"mcp_servers.transport must be one of {list(MCP_TRANSPORTS)}, got {text!r}"
        )
    return text


def validate_server_status(status: str | None) -> str:
    """Return the status, or raise `ValueError` listing the known ones."""
    text = str(status or "").strip()
    if text not in MCP_SERVER_STATUSES:
        raise ValueError(
            f"mcp_servers.status must be one of {list(MCP_SERVER_STATUSES)}, got {text!r}"
        )
    return text


def truncate_error(text: str | None) -> str | None:
    """Bound a stored error message; `None` stays `None` (no error recorded)."""
    if text is None:
        return None
    cleaned = str(text).strip()
    return cleaned[:MAX_ERROR_CHARS] or None


@dataclass(frozen=True, slots=True)
class McpServerRecord:
    """One `mcp_servers` row, detached from the session that produced it.

    `headers_encrypted` is the Fernet token, never plaintext: the store does not know
    how to decrypt it and the API never returns it unmasked.

    A record is also the *input* shape for `McpServerStore.create()`, so the required
    fields accept empty strings and the store validates them (`owner_user_id`,
    `name`, `transport`, `status`): a missing owner or a Chinese slug fails there with
    a message that names the rule, not with a `TypeError` at the call site.  `id` is
    empty on a record that is not stored yet; the store mints a UUID for it.
    """

    id: str = ""
    owner_user_id: str = ""
    name: str = ""
    display_name: str | None = None
    description: str = ""
    transport: str = "streamable_http"
    url: str = ""
    headers_encrypted: str | None = None
    verify_tls: bool = True
    enabled: bool = True
    status: str = "pending"
    reachable: bool = False
    last_error: str | None = None
    last_connected_at: datetime | None = None
    last_tools_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def is_active(self) -> bool:
        """True when this server may serve discoveries and calls right now."""
        return bool(self.enabled and self.status == "active" and self.reachable)


__all__ = [
    "MAX_ERROR_CHARS",
    "MAX_SERVER_NAME_CHARS",
    "MCP_SERVER_STATUSES",
    "MCP_TRANSPORTS",
    "McpServerRecord",
    "McpServerStatus",
    "McpTransport",
    "SERVER_NAME_PATTERN",
    "is_valid_server_name",
    "truncate_error",
    "validate_server_name",
    "validate_server_status",
    "validate_transport",
]
