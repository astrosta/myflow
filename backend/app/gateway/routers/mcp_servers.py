"""MCP server management API. Prefix /mcp-servers, mounted under /api.

MCP servers are user-private: every route passes the caller's user id into the
owner-scoped service, and another owner's id is a 404 rather than a 403.  The
browser only ever receives masked headers; the encrypted column never leaves the
service.

Static routes (/status, /reindex) are declared before /{server_id}, otherwise a
request for "status" would be matched as a server id.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from agentkit.mcp.errors import McpServerNotFound
from agentkit.mcp.service import McpServerWrite, url_has_query, url_without_query

from backend.app.gateway.mcp_servers_deps import (
    get_mcp_runtime,
    get_mcp_server_service,
    require_mcp_reader,
    require_mcp_writer,
)
from backend.app.gateway.models.mcp_servers import (
    McpServerCreate,
    McpServerDetail,
    McpServerListResponse,
    McpServerReindexResponse,
    McpServerStateRequest,
    McpServerStatusResponse,
    McpServerSummary,
    McpServerToolsResponse,
    McpServerToolSummary,
    McpServerUpdate,
)
from backend.app.gateway.run_models import RunCaller

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

McpReader = Annotated[RunCaller, Depends(require_mcp_reader)]
McpWriter = Annotated[RunCaller, Depends(require_mcp_writer)]
Service = Annotated[Any, Depends(get_mcp_server_service)]
Runtime = Annotated[Any, Depends(get_mcp_runtime)]


def _summary(record: Any, tool_count: int = 0) -> McpServerSummary:
    return McpServerSummary(
        id=str(record.id),
        name=str(record.name),
        display_name=getattr(record, "display_name", None),
        description=str(getattr(record, "description", "") or ""),
        url=url_without_query(getattr(record, "url", "")),
        url_has_query=url_has_query(getattr(record, "url", "")),
        transport=str(getattr(record, "transport", "streamable_http")),
        verify_tls=bool(getattr(record, "verify_tls", True)),
        enabled=bool(getattr(record, "enabled", True)),
        status=str(getattr(record, "status", "pending")),
        reachable=bool(getattr(record, "reachable", False)),
        tool_count=tool_count,
        last_error=getattr(record, "last_error", None),
        last_connected_at=getattr(record, "last_connected_at", None),
        last_tools_sync_at=getattr(record, "last_tools_sync_at", None),
        created_at=getattr(record, "created_at", None),
        updated_at=getattr(record, "updated_at", None),
    )


async def _tool_count(service: Any, record: Any, owner_user_id: str) -> int:
    try:
        return len(await service.list_tools(record.id, owner_user_id))
    except McpServerNotFound:
        return 0


async def _detail(
    service: Any, record: Any, owner_user_id: str
) -> McpServerDetail:
    count = await _tool_count(service, record, owner_user_id)
    return McpServerDetail(
        **_summary(record, count).model_dump(),
        headers=service.masked_headers(record),
    )


def _create_write(payload: McpServerCreate) -> McpServerWrite:
    return McpServerWrite(
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        url=payload.url,
        transport=payload.transport,
        headers=payload.headers,
        verify_tls=payload.verify_tls,
        enabled=payload.enabled,
    )


def _update_write(payload: McpServerUpdate) -> McpServerWrite:
    fields: dict[str, Any] = {}
    for name in payload.model_fields_set:
        fields[name] = getattr(payload, name)
    return McpServerWrite(**fields)


# --------------------------------------------------------------------------- #
# Static routes first: /status and /reindex are not server ids.
# --------------------------------------------------------------------------- #


@router.get("/status", response_model=McpServerStatusResponse)
async def server_status(
    caller: McpReader, service: Service, runtime: Runtime
) -> McpServerStatusResponse:
    """Counts plus the *runtime's* sync/index state for this caller.

    The service alone can only count rows, so it cannot see whether the last pass
    failed, whether the index is degraded, or how many rows are really callable.
    The runtime is the process that owns those facts; the service projection stays
    as the fallback for a deployment whose runtime was never published.
    """
    raw = await _status_from_runtime(runtime, caller.user_id)
    if raw is None:
        raw = await service.status(caller.user_id)
    return McpServerStatusResponse.model_validate(raw)


async def _status_from_runtime(
    runtime: Any, owner_user_id: str
) -> dict[str, Any] | None:
    status = getattr(runtime, "status", None)
    if not callable(status):
        return None
    try:
        return await status(owner_user_id)
    except Exception:  # noqa: BLE001 - a status read must never 500 the page
        _logger.warning("MCP runtime status read failed; using the service view")
        return None


@router.post("/reindex", response_model=McpServerReindexResponse)
async def reindex_servers(
    caller: McpWriter, service: Service
) -> McpServerReindexResponse:
    stats = await service.reindex(caller.user_id)
    return McpServerReindexResponse.model_validate(stats)


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


@router.get("", response_model=McpServerListResponse)
async def list_servers(caller: McpReader, service: Service) -> McpServerListResponse:
    records = await service.list_servers(caller.user_id)
    servers = [
        _summary(record, await _tool_count(service, record, caller.user_id))
        for record in records
    ]
    return McpServerListResponse(servers=servers, count=len(servers))


@router.post("", response_model=McpServerDetail)
async def create_server(
    payload: McpServerCreate, caller: McpWriter, service: Service
) -> McpServerDetail:
    record = await service.create_server(caller.user_id, _create_write(payload))
    return await _detail(service, record, caller.user_id)


# --------------------------------------------------------------------------- #
# One server
# --------------------------------------------------------------------------- #


@router.get("/{server_id}", response_model=McpServerDetail)
async def get_server(
    server_id: str, caller: McpReader, service: Service
) -> McpServerDetail:
    record = await service.get_server(server_id, caller.user_id)
    return await _detail(service, record, caller.user_id)


@router.put("/{server_id}", response_model=McpServerDetail)
async def update_server(
    server_id: str,
    payload: McpServerUpdate,
    caller: McpWriter,
    service: Service,
) -> McpServerDetail:
    record = await service.update_server(
        server_id, caller.user_id, _update_write(payload)
    )
    return await _detail(service, record, caller.user_id)


@router.delete("/{server_id}", status_code=204)
async def delete_server(
    server_id: str, caller: McpWriter, service: Service
) -> Response:
    await service.delete_server(server_id, caller.user_id)
    return Response(status_code=204)


@router.post("/{server_id}/state", response_model=McpServerDetail)
async def set_server_state(
    server_id: str,
    payload: McpServerStateRequest,
    caller: McpWriter,
    service: Service,
) -> McpServerDetail:
    record = await service.set_enabled(server_id, caller.user_id, payload.enabled)
    return await _detail(service, record, caller.user_id)


@router.post("/{server_id}/refresh", response_model=McpServerDetail)
async def refresh_server(
    server_id: str, caller: McpWriter, service: Service
) -> McpServerDetail:
    record = await service.refresh_server(server_id, caller.user_id)
    return await _detail(service, record, caller.user_id)


def _tool_summary(row: Any, *, include_schema: bool) -> McpServerToolSummary:
    """Project one mcp_tools row exactly as the caller asked to see it.

    include_schema=true carries the row's stored input_schema verbatim (it is the
    same object the agent hands the model, so nothing is re-derived); the default
    clears it, and the model then omits the key, which keeps the browser payload
    byte-for-byte what it was before the flag existed.
    """
    summary = McpServerToolSummary.model_validate(row)
    if not include_schema:
        summary.input_schema = None
    return summary


@router.get("/{server_id}/tools", response_model=McpServerToolsResponse)
async def list_server_tools(
    server_id: str,
    caller: McpReader,
    service: Service,
    include_schema: bool = Query(default=False),
) -> McpServerToolsResponse:
    """This caller's tools for one server; the stored schema is opt-in.

    The default stays schema-free so the management UI payload does not grow: only
    a caller that has to build tool arguments needs mcp_tools.input_schema.
    """
    rows = await service.list_tools(server_id, caller.user_id)
    tools = [_tool_summary(row, include_schema=include_schema) for row in rows]
    return McpServerToolsResponse(tools=tools, count=len(tools))


__all__ = [
    "create_server",
    "delete_server",
    "get_server",
    "list_server_tools",
    "list_servers",
    "reindex_servers",
    "refresh_server",
    "router",
    "server_status",
    "set_server_state",
    "update_server",
]
