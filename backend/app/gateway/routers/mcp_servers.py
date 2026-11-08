"""MCP server management API. Prefix /mcp-servers, mounted under /api.

MCP servers are user-private: every route passes the caller's user id into the
owner-scoped service, and another owner's id is a 404 rather than a 403.  The
browser only ever receives masked headers; the encrypted column never leaves the
service.

Static routes (/status, /reindex) are declared before /{server_id}, otherwise a
request for "status" would be matched as a server id.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from agentkit.mcp.errors import McpServerNotFound
from agentkit.mcp.service import McpServerWrite, url_has_query, url_without_query

from backend.app.gateway.mcp_servers_deps import (
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

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

McpReader = Annotated[RunCaller, Depends(require_mcp_reader)]
McpWriter = Annotated[RunCaller, Depends(require_mcp_writer)]
Service = Annotated[Any, Depends(get_mcp_server_service)]


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
async def server_status(caller: McpReader, service: Service) -> McpServerStatusResponse:
    raw = await service.status(caller.user_id)
    return McpServerStatusResponse.model_validate(raw)


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


@router.get("/{server_id}/tools", response_model=McpServerToolsResponse)
async def list_server_tools(
    server_id: str, caller: McpReader, service: Service
) -> McpServerToolsResponse:
    rows = await service.list_tools(server_id, caller.user_id)
    tools = [McpServerToolSummary.model_validate(row) for row in rows]
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
