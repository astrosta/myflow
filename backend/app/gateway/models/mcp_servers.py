"""Wire models for /api/mcp-servers.

The write/state models forbid extra keys so a client cannot smuggle a rename or an
enabled flag into PUT; the server name and the enable switch have their own
lifecycle.  Detail is the only model that carries headers, and those are always
already masked by the service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
)

McpTransportName = Literal["streamable_http", "sse"]


class McpServerSummary(BaseModel):
    """One row in the management list; deliberately has no header field."""

    id: str
    name: str
    display_name: str | None = None
    description: str = ""
    #: Query strings are never returned; this says whether the stored URL has one.
    url: str
    url_has_query: bool = False
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
    #: mcp_servers.display_name is VARCHAR(255); a longer value must be a 422 here
    #: and not a Postgres error after SQLite let it through.
    display_name: str | None = Field(default=None, max_length=255)
    description: str = ""
    url: str
    transport: McpTransportName = "streamable_http"
    headers: dict[str, str] = Field(default_factory=dict)
    verify_tls: bool = True
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """A partial edit. name/enabled are intentionally absent and therefore 422."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    url: str | None = None
    transport: McpTransportName | None = None
    headers: dict[str, str] | None = None
    verify_tls: bool | None = None
    #: Drop the stored URL query string instead of preserving it.  The API never
    #: returns a query string, so without this a stored one could never be removed.
    clear_query: bool = False

    @field_validator("verify_tls", mode="before")
    @classmethod
    def _verify_tls_must_be_boolean(cls, value: object) -> object:
        """An omitted field stays unset; an explicit null is a downgrade attempt."""
        if value is None:
            raise ValueError(
                "verify_tls must be true or false; omit it to keep the current value"
            )
        return value


class McpServerStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class McpServerToolSummary(BaseModel):
    """Read-only projection of one mcp_tools row; a secret never rides along.

    The stored input schema is opt-in (GET /tools?include_schema=true) because the
    browser list never reads it, while the evaluation dataset builder derives a
    sample's arguments from exactly that object.  A schema is not a secret -- the
    agent hands one to the model on every tool call -- so it needs no masking; it
    is hidden by default only to keep the UI payload small.
    """

    model_config = ConfigDict(extra="ignore", from_attributes=True)

    name: str
    call_name: str
    display_name: str | None = None
    description: str = ""
    parameters: list[str] = Field(default_factory=list)
    callable: bool = False
    enabled: bool = True
    #: mcp_tools.input_schema, verbatim, and only when the caller asked for it.
    input_schema: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_an_absent_schema(self, handler: Any) -> dict[str, Any]:
        """Drop the key instead of sending null.

        include_schema=false must return byte-for-byte the pre-opt-in payload, so an
        unset schema is absent from the JSON rather than present as a null.
        """
        data = handler(self)
        if self.input_schema is None:
            data.pop("input_schema", None)
        return data


class McpServerToolsResponse(BaseModel):
    tools: list[McpServerToolSummary] = Field(default_factory=list)
    count: int = 0


class McpServerStatusResponse(BaseModel):
    """The caller's server and tool counts plus this process's sync state.

    The counts alone (the original Task 3 projection) hid every operational fact:
    a deployment whose index was broken, or whose last pass failed, looked exactly
    like a healthy one.  The runtime fills these in; the service fallback leaves
    them at their defaults when no runtime is published.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    servers_total: int = 0
    servers_active: int = 0
    servers_pending: int = 0
    servers_error: int = 0
    servers_disabled: int = 0
    servers_reachable: int = 0
    tools_total: int = 0
    #: Rows that are actually callable - not a synonym for tools_total.
    indexed_tools: int = 0
    last_sync_at: datetime | None = None
    last_error: str | None = None
    #: "ready" | "degraded" | "syncing" | "disabled"
    sync: str | None = None
    #: "ready" | "degraded" | "disabled"
    index: str | None = None
    last_sync_reason: str | None = None
    last_sync_duration_ms: int | None = None


class McpServerReindexResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
