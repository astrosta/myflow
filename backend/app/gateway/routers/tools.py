"""ContextForge tool management API. Prefix /tools, mounted under /api."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from agentkit.authz import ADMIN_ROLE
from agentkit.config import get_app_config
from agentkit.contextforge.admin import ContextForgeAdminClient
from agentkit.contextforge.models import ContextForgeTool, ToolWrite

from backend.app.gateway.deps import require_scope
from backend.app.gateway.errors import ContextForgeDisabled, InvalidToolHeaderAPI
from backend.app.gateway.models.tools import (
    ToolDetail,
    ToolListResponse,
    ToolStateRequest,
    ToolSummary,
)
from backend.app.gateway.tools_wire import (
    InvalidToolHeader,
    mask_headers,
    restore_masked_headers,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/tools", tags=["tools"])

AdminCaller = Annotated[RunCaller, Depends(require_scope(ADMIN_ROLE))]


def get_contextforge_admin_client(request: Request) -> ContextForgeAdminClient:
    config = get_app_config().contextforge
    if not config.enabled:
        raise ContextForgeDisabled("ContextForge tool management is disabled")
    client = getattr(request.app.state, "contextforge_admin_client", None)
    if client is not None:
        return client
    client = ContextForgeAdminClient(config)
    request.app.state.contextforge_admin_client = client
    return client


def _summary(tool: ContextForgeTool) -> ToolSummary:
    return ToolSummary(
        id=tool.id,
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description,
        url=tool.url,
        enabled=tool.enabled,
        reachable=tool.reachable,
        integration_type=tool.integration_type,
        request_type=tool.request_type,
        tags=list(tool.tags),
        visibility=tool.visibility,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _detail(tool: ContextForgeTool) -> ToolDetail:
    return ToolDetail(
        **_summary(tool).model_dump(),
        headers=mask_headers(tool.headers),
        input_schema=tool.input_schema,
        annotations=tool.annotations,
    )


@router.get("", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    caller: AdminCaller,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = None,
    enabled: bool | None = None,
    integration_type: str | None = None,
) -> ToolListResponse:
    del caller
    try:
        skip = int(cursor) if cursor else 0
    except ValueError:
        skip = 0
    if skip < 0:
        skip = 0
    client = get_contextforge_admin_client(request)
    tools = await client.list_tools(limit=limit, skip=skip, include_inactive=True)
    filtered = list(tools)
    if q and q.strip():
        needle = q.strip().casefold()
        filtered = [
            tool
            for tool in filtered
            if needle in tool.name.casefold()
            or needle in (tool.display_name or "").casefold()
            or needle in tool.description.casefold()
            or any(needle in tag.casefold() for tag in tool.tags)
        ]
    if enabled is not None:
        filtered = [tool for tool in filtered if tool.enabled is enabled]
    if integration_type:
        filtered = [
            tool
            for tool in filtered
            if tool.integration_type.casefold() == integration_type.casefold()
        ]
    next_cursor = str(skip + len(tools)) if len(tools) >= limit else None
    return ToolListResponse(
        tools=[_summary(tool) for tool in filtered],
        count=len(filtered),
        next_cursor=next_cursor,
    )


@router.get("/{tool_id}", response_model=ToolDetail)
async def get_tool(
    tool_id: str,
    request: Request,
    caller: AdminCaller,
) -> ToolDetail:
    del caller
    client = get_contextforge_admin_client(request)
    return _detail(await client.get_tool(tool_id))


@router.post("", response_model=ToolDetail)
async def create_tool(
    body: ToolWrite,
    request: Request,
    caller: AdminCaller,
) -> ToolDetail:
    del caller
    client = get_contextforge_admin_client(request)
    tool = await client.create_tool(body)
    if body.enabled is False and tool.enabled:
        tool = await client.set_tool_state(tool.id, False)
    return _detail(tool)


@router.put("/{tool_id}", response_model=ToolDetail)
async def update_tool(
    tool_id: str,
    body: ToolWrite,
    request: Request,
    caller: AdminCaller,
) -> ToolDetail:
    del caller
    client = get_contextforge_admin_client(request)
    current = await client.get_tool(tool_id)
    try:
        restored = restore_masked_headers(body.headers, current.headers)
    except InvalidToolHeader as exc:
        raise InvalidToolHeaderAPI(str(exc)) from None
    updated = body.model_copy(update={"headers": restored})
    tool = await client.update_tool(tool_id, updated)
    if tool.enabled is not body.enabled:
        tool = await client.set_tool_state(tool_id, body.enabled)
    return _detail(tool)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: str,
    request: Request,
    caller: AdminCaller,
) -> Response:
    del caller
    client = get_contextforge_admin_client(request)
    await client.delete_tool(tool_id)
    return Response(status_code=204)


@router.post("/{tool_id}/state", response_model=ToolDetail)
async def set_tool_state(
    tool_id: str,
    body: ToolStateRequest,
    request: Request,
    caller: AdminCaller,
) -> ToolDetail:
    del caller
    client = get_contextforge_admin_client(request)
    return _detail(await client.set_tool_state(tool_id, body.enabled))
