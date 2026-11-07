"""ContextForge tool management API. Prefix /tools, mounted under /api."""

from __future__ import annotations

from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, Query, Request, Response
from agentkit.contextforge.models import (
    ContextForgeError,
    ContextForgeNotFound,
    ContextForgeTool,
    ToolWrite,
)
from agentkit.contextforge.ownership import (
    can_claim,
    can_manage,
    can_view,
    strip_owner,
    with_owner,
)

from backend.app.gateway.errors import ContextForgeDisabled, InvalidToolHeaderAPI
from backend.app.gateway.tools_deps import get_contextforge_admin_client, require_tools_scope
from agentkit.contextforge.runtime import get_contextforge_runtime

from backend.app.gateway.models.tools import (
    ToolDetail,
    ToolListResponse,
    ToolStateRequest,
    ToolStatusResponse,
    ToolSummary,
)
from backend.app.gateway.tools_wire import (
    InvalidToolHeader,
    mask_headers,
    restore_masked_headers,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/tools", tags=["tools"])

ToolReader = Annotated[RunCaller, Depends(require_tools_scope("tools:read"))]
ToolWriter = Annotated[RunCaller, Depends(require_tools_scope("tools:write"))]


class ToolSearchUnavailable(ContextForgeError):
    """The search preview has no service behind it yet (Task 6 wires it).

    It subclasses ContextForgeError so the response keeps the shared coded error
    body: errors.py registers that handler for the base class and Starlette
    dispatches on the exception's MRO, so this arrives as
    `{detail, code, trace_id}` with a 503 instead of an unhandled 500.
    """

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "tool_search_unavailable"


async def _request_refresh(request: Request, reason: str = "admin") -> None:
    runtime = get_contextforge_runtime()
    if runtime is not None:
        await runtime.request_refresh(reason)


def _flags(tool: ContextForgeTool, caller: RunCaller) -> tuple[bool, bool, bool]:
    if caller.user_id is None:
        return True, False, True
    owned = tool.owner is not None and tool.owner == caller.user_id
    claimable = tool.owner is None
    return owned, claimable, owned


def _summary(tool: ContextForgeTool, caller: RunCaller) -> ToolSummary:
    owned, claimable, can_edit = _flags(tool, caller)
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
        owned=owned,
        claimable=claimable,
        can_edit=can_edit,
    )


def _detail(tool: ContextForgeTool, caller: RunCaller) -> ToolDetail:
    return ToolDetail(
        **_summary(tool, caller).model_dump(),
        headers=mask_headers(tool.headers),
        input_schema=tool.input_schema,
        annotations=strip_owner(tool.annotations),
    )


def _visible(tool: ContextForgeTool, caller: RunCaller) -> None:
    if not can_view(tool.owner, caller.user_id):
        raise ContextForgeNotFound("tool not found")


def _manageable(tool: ContextForgeTool, caller: RunCaller) -> None:
    if not can_manage(tool.owner, caller.user_id):
        raise ContextForgeNotFound("tool not found")


def _claimable(tool: ContextForgeTool, caller: RunCaller) -> None:
    if not can_claim(tool.owner, caller.user_id):
        raise ContextForgeNotFound("tool not found")


def _owner_for_write(tool: ContextForgeTool, caller: RunCaller) -> str | None:
    if caller.user_id is None:
        return tool.owner
    return tool.owner or caller.user_id


def _with_owner_annotations(tool: ContextForgeTool, caller: RunCaller, annotations: dict) -> dict:
    return with_owner(annotations, _owner_for_write(tool, caller))


async def _rewrite_annotations(
    client,
    tool: ContextForgeTool,
    annotations: dict,
) -> ContextForgeTool:
    data = ToolWrite(
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description,
        integration_type=tool.integration_type,
        request_type=tool.request_type,
        url=tool.url,
        headers=tool.headers,
        input_schema=tool.input_schema,
        annotations=annotations,
        tags=list(tool.tags),
        visibility=tool.visibility,
        enabled=tool.enabled,
    )
    return await client.update_tool(tool.id, data)


@router.get("", response_model=ToolListResponse)
async def list_tools(
    request: Request,
    caller: ToolReader,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = None,
    enabled: bool | None = None,
    integration_type: str | None = None,
) -> ToolListResponse:
    try:
        skip = int(cursor) if cursor else 0
    except ValueError:
        skip = 0
    if skip < 0:
        skip = 0
    client = get_contextforge_admin_client(request)
    tools = await client.list_tools(limit=limit, skip=skip, include_inactive=True)
    filtered = [tool for tool in tools if can_view(tool.owner, caller.user_id)]
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
        tools=[_summary(tool, caller) for tool in filtered],
        count=len(filtered),
        next_cursor=next_cursor,
    )


@router.get("/status", response_model=ToolStatusResponse)
async def tool_status(
    request: Request,
    caller: ToolReader,
) -> ToolStatusResponse:
    del request
    runtime = get_contextforge_runtime()
    if runtime is None:
        return ToolStatusResponse(enabled=False)
    # status() reads the authoritative store, so it is async; the counts are
    # scoped to the caller - /api/tools is a per-user surface, and a plain user
    # must not read the catalog-wide totals.
    status = await runtime.status(owner_user_id=caller.user_id)
    return ToolStatusResponse.model_validate(status)


@router.post("/reindex")
async def reindex_tools(
    request: Request,
    caller: ToolWriter,
) -> dict[str, object]:
    del caller
    runtime = get_contextforge_runtime()
    if runtime is None:
        raise ContextForgeDisabled("ContextForge tool management is disabled")
    stats = await runtime.reindex()
    return {
        "scanned": stats.scanned,
        "changed": stats.changed,
        "upserted": stats.upserted,
        "deleted": stats.deleted,
        "duration_ms": stats.duration_ms,
        "error": stats.error,
    }


@router.get("/search")
async def search_tools_preview(
    request: Request,
    caller: ToolReader,
    q: str = Query(min_length=1, max_length=4000),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict[str, object]:
    # The in-memory tool snapshot this preview used to search is gone with Task 5,
    # and Task 6 is what wires the new ToolSearchService into the runtime.  Until
    # then the route says "unavailable" out loud: an empty result list would read
    # as "no tool matches your query", which is indistinguishable from a real miss.
    del request, caller, q, limit
    raise ToolSearchUnavailable(
        "tool search is not available: no tool search service is wired into this build"
    )


@router.get("/{tool_id}", response_model=ToolDetail)
async def get_tool(
    tool_id: str,
    request: Request,
    caller: ToolReader,
) -> ToolDetail:
    client = get_contextforge_admin_client(request)
    tool = await client.get_tool(tool_id)
    _visible(tool, caller)
    return _detail(tool, caller)


@router.post("", response_model=ToolDetail)
async def create_tool(
    body: ToolWrite,
    request: Request,
    caller: ToolWriter,
) -> ToolDetail:
    client = get_contextforge_admin_client(request)
    owner = caller.user_id
    created = body.model_copy(
        update={"annotations": with_owner(body.annotations, owner)}
    )
    tool = await client.create_tool(created)
    if body.enabled is False and tool.enabled:
        tool = await client.set_tool_state(tool.id, False)
    await _request_refresh(request)
    return _detail(tool, caller)


@router.put("/{tool_id}", response_model=ToolDetail)
async def update_tool(
    tool_id: str,
    body: ToolWrite,
    request: Request,
    caller: ToolWriter,
) -> ToolDetail:
    client = get_contextforge_admin_client(request)
    current = await client.get_tool(tool_id)
    _manageable(current, caller)
    try:
        restored = restore_masked_headers(body.headers, current.headers)
    except InvalidToolHeader as exc:
        raise InvalidToolHeaderAPI(str(exc)) from None
    annotations = _with_owner_annotations(current, caller, body.annotations)
    updated = body.model_copy(update={"headers": restored, "annotations": annotations})
    tool = await client.update_tool(tool_id, updated)
    if tool.enabled is not body.enabled:
        tool = await client.set_tool_state(tool_id, body.enabled)
    await _request_refresh(request)
    return _detail(tool, caller)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: str,
    request: Request,
    caller: ToolWriter,
) -> Response:
    client = get_contextforge_admin_client(request)
    current = await client.get_tool(tool_id)
    _manageable(current, caller)
    await client.delete_tool(tool_id)
    await _request_refresh(request)
    return Response(status_code=204)


@router.post("/{tool_id}/claim", response_model=ToolDetail)
async def claim_tool(
    tool_id: str,
    request: Request,
    caller: ToolWriter,
) -> ToolDetail:
    client = get_contextforge_admin_client(request)
    current = await client.get_tool(tool_id)
    _claimable(current, caller)
    tool = current
    if current.owner is None and caller.user_id is not None:
        tool = await _rewrite_annotations(
            client,
            current,
            with_owner(current.annotations, caller.user_id),
        )
        await _request_refresh(request)
    return _detail(tool, caller)


@router.post("/{tool_id}/state", response_model=ToolDetail)
async def set_tool_state(
    tool_id: str,
    body: ToolStateRequest,
    request: Request,
    caller: ToolWriter,
) -> ToolDetail:
    client = get_contextforge_admin_client(request)
    current = await client.get_tool(tool_id)
    _manageable(current, caller)
    tool = await client.set_tool_state(tool_id, body.enabled)
    await _request_refresh(request)
    return _detail(tool, caller)