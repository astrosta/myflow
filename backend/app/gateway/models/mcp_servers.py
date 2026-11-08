"""Wire models for /api/mcp-servers.

The write/state models forbid extra keys so a client cannot smuggle a rename or an
enabled flag into PUT; the server name and the enable switch have their own
lifecycle.  Detail is the only model that carries headers, and those are always
already masked by the service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

McpTransportName = Literal["streamable_http", "sse"]


class McpServerSummary(BaseModel):
    """One row in the management list; deliberately has no header field."""

    id: str
    name: str
    display_name: str | None = None
    description: str = ""
    url: str
    transport: McpTransportName
    verify_tls: bool = True
    enabled: bool = True
    status: str = "pending"
    reachable: bool = False
    tool_count: int = 0
    last_error: str | None = None
    last_connected_at: datetime | None = None
    last_tools_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class McpServerDetail(McpServerSummary):
    #: Masked only: the service decrypts the stored map and replaces secrets.
    headers: dict[str, str] = Field(default_factory=dict)


class McpServerListResponse(BaseModel):
    servers: list[McpServerSummary] = Field(default_factory=list)
    count: int = 0


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    description: str = ""
    url: str
    transport: McpTransportName = "streamable_http"
    headers: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """A partial edit. name/enabled are intentionally absent and therefore 422."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    description: str | None = None
    url: str | None = None
    transport: McpTransportName | None = None
    headers: dict[str, str] | None = None
    verify_tls: bool | None = None


class McpServerStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class McpServerToolSummary(BaseModel):
    """Read-only projection of one mcp_tools row; no schema or secret rides along."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    name: str
    call_name: str
    display_name: str | None = None
    description: str = ""
    parameters: list[str] = Field(default_factory=list)
    callable: bool = False
    enabled: bool = True


class McpServerToolsResponse(BaseModel):
    tools: list[McpServerToolSummary] = Field(default_factory=list)
    count: int = 0


class McpServerStatusResponse(BaseModel):
    """The caller's server and tool counts; the runtime may add fields later."""

    enabled: bool = True
    servers_total: int = 0
    servers_active: int = 0
    servers_pending: int = 0
    servers_error: int = 0
    servers_disabled: int = 0
    servers_reachable: int = 0
    tools_total: int = 0
    indexed_tools: int = 0
    last_sync_at: datetime | None = None
    last_error: str | None = None


class McpServerReindexResponse(BaseModel):
    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    failed: int = 0
    duration_ms: int = 0
    error: str | None = None


__all__ = [
    "McpServerCreate",
    "McpServerDetail",
    "McpServerListResponse",
    "McpServerReindexResponse",
    "McpServerStateRequest",
    "McpServerStatusResponse",
    "McpServerSummary",
    "McpServerToolSummary",
    "McpServerToolsResponse",
    "McpServerUpdate",
    "McpTransportName",
]
