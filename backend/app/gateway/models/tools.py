"""Wire models for the ContextForge-backed /api/tools surface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolSummary(BaseModel):
    id: str
    name: str
    display_name: str | None = None
    description: str = ""
    url: str | None = None
    enabled: bool = True
    reachable: bool = True
    integration_type: str = "REST"
    request_type: str = "SSE"
    tags: list[str] = Field(default_factory=list)
    visibility: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    shadowed_by: str | None = None
    index_status: str = "unknown"
    owned: bool = False
    claimable: bool = False
    can_edit: bool = False


class ToolDetail(ToolSummary):
    headers: dict[str, str] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    tools: list[ToolSummary]
    count: int
    next_cursor: str | None = None


class ToolStateRequest(BaseModel):
    enabled: bool


class ToolChannelsStatus(BaseModel):
    keyword: str = "disabled"
    embedding: str = "disabled"
    rerank: str = "disabled"


class ToolStatusResponse(BaseModel):
    enabled: bool
    reachable: bool = False
    auth_ok: bool = False
    admin_events: str = "disabled"
    sync: str = "disabled"
    index: str = "disabled"
    channels: ToolChannelsStatus = Field(default_factory=ToolChannelsStatus)
    indexed_tools: int = 0
    total_tools: int = 0
    last_sync_at: str | None = None
    last_sync_reason: str | None = None
    last_error: str | None = None
