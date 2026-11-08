"""Header masking and restoration for /api/mcp-servers.

The browser never sees a raw header value. This module keeps the wire-level
helpers thin and imports the canonical masking rules from agentkit.mcp.service so
the service and the API cannot drift; the only addition is the HTTP-oriented
exception type for a masked value with no stored counterpart.
"""

from __future__ import annotations

from collections.abc import Mapping

from agentkit.mcp.errors import McpServerValidationError
from agentkit.mcp.service import (
    MASK,
    is_sensitive_header,
    mask_headers,
    restore_masked_headers as _restore_masked_headers,
)


class InvalidMcpServerHeader(ValueError):
    """A masked header cannot be restored or a header value is unsafe."""


def restore_masked_headers(
    incoming: Mapping[str, str] | None,
    stored: Mapping[str, str] | None,
) -> dict[str, str]:
    try:
        return _restore_masked_headers(incoming, stored)
    except McpServerValidationError as exc:
        raise InvalidMcpServerHeader(str(exc)) from None


__all__ = [
    "InvalidMcpServerHeader",
    "MASK",
    "is_sensitive_header",
    "mask_headers",
    "restore_masked_headers",
]
