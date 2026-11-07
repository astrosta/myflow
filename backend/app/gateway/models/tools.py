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
    """One process's view of every link between ContextForge and the catalog.

    The fields mirror ContextForgeRuntime.status: the runtime tracks them, this
    model is the wire shape, and pydantic silently drops any key it does not
    declare - so a field the page needs must be named here or it never leaves
    the gateway.
    """

    enabled: bool
    reachable: bool = False
    auth_ok: bool = False
    #: /admin/events subscriber: connected | connecting | reconnecting | error | disabled.
    admin_events: str = "disabled"
    #: State of the local-midnight fallback sync task: running | stopped | disabled.
    daily: str = "disabled"
    #: ready | syncing | degraded | disabled.
    sync: str = "disabled"
    #: ready | degraded | disabled.
    index: str = "disabled"
    channels: ToolChannelsStatus = Field(default_factory=ToolChannelsStatus)
    indexed_tools: int = 0
    total_tools: int = 0
    last_sync_at: str | None = None
    last_sync_reason: str | None = None
    #: Wall time of the last pass, so a "syncing" that never ends is visible.
    last_sync_duration_ms: int | None = None
    last_error: str | None = None
    #: The tool-error retry: when it ran, which tool, and what it concluded.
    last_retry_at: str | None = None
    last_retry_tool: str | None = None
    last_retry_result: str | None = None


class ToolSearchHit(BaseModel):
    """One retrieval card: the database row behind a Milvus hit.

    parameters/tags/description only exist in contextforge_tools, so a hit that
    carries them proves the preview ran the database confirmation step and not
    just the vector recall.
    """

    tool_id: str
    tool_name: str
    #: The model/policy-facing name (cf__<name>), not the ContextForge name.
    call_name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    score: float = 0.0
    schema_truncated: bool = False


class ToolSearchRetrieval(BaseModel):
    """What retrieval actually did: which channels ran, and what it warned."""

    #: milvus when the search answered from recall, unavailable when it could not.
    index: str = "unavailable"
    channels: list[str] = Field(default_factory=list)
    candidates: int = 0
    reranked: bool = False
    warnings: list[str] = Field(default_factory=list)


class ToolSearchPreview(BaseModel):
    query: str
    results: list[ToolSearchHit] = Field(default_factory=list)
    retrieval: ToolSearchRetrieval = Field(default_factory=ToolSearchRetrieval)
