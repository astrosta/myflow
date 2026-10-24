"""Thread create, lookup, list, and checkpoint state."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from agentkit.client import DEFAULT_USER_ID
from agentkit.persistence.thread_meta.base import ThreadMetaStore
from backend.app.gateway.deps import RunServices, get_run_services, get_thread_store, require_scope
from backend.app.gateway.checkpoint_lineage import (
    _checkpoint_id_from_body,
    apply_state_update,
    list_branches,
    walk_history,
)
from backend.app.gateway.models.request import (
    ThreadCompactRequest,
    ThreadCreateRequest,
    ThreadId,
    ThreadPatchRequest,
    ThreadSearchRequest,
    ThreadStateUpdateRequest,
)
from backend.app.gateway.models.response import (
    BranchListResponse,
    DeletedResponse,
    HistoryEntry,
    MessageResponse,
    Page,
    ThreadCompactResponse,
    ThreadResponse,
    ThreadStateResponse,
    message_to_wire,
    thread_to_wire,
)
from backend.app.gateway.pagination import (
    MessagePageParams,
    PageParams,
    message_page_params,
    page_params,
    page_response,
    trim_message_page,
)
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.services import ThreadNotFound, compact_thread_context, delete_thread, reserve_checkpoint_write
from backend.app.gateway.state_reader import read_thread_state
from backend.app.gateway.thread_access import require_visible_thread

router = APIRouter(prefix="/threads", tags=["threads"])


def _owner(caller: RunCaller) -> str:
    """M3's anonymous caller has no user id. The store refuses to persist NULL."""
    return caller.user_id if caller.user_id is not None else DEFAULT_USER_ID


def _thread_page(rows: list[dict], *, limit: int) -> dict:
    page, has_more = rows[:limit], len(rows) > limit
    wired = [thread_to_wire(row) for row in page]
    cursors = [row["cursor"] for row in page]
    return page_response(wired, has_more=has_more, next_cursor=cursors[-1] if has_more and cursors else None)


@router.post("/search", response_model=Page[ThreadResponse])
async def search_threads(
    body: ThreadSearchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> Page[ThreadResponse]:
    rows = await store.search_page(
        user_id=caller.user_id,
        metadata=body.metadata,
        status=body.status,
        limit=body.limit + 1,
        cursor=body.cursor,
        order=body.order,
    )
    return _thread_page(rows, limit=body.limit)


@router.post("", status_code=201, response_model=ThreadResponse)
async def create_thread(
    body: ThreadCreateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> ThreadResponse:
    thread_id = body.thread_id or uuid4().hex
    row = await store.create(
        thread_id,
        assistant_id=body.assistant_id,
        user_id=_owner(caller),
        metadata=body.metadata,
    )
    return thread_to_wire(row)


@router.get("", response_model=Page[ThreadResponse])
async def list_threads(
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    params: Annotated[PageParams, Depends(page_params)],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> Page[ThreadResponse]:
    rows = await store.search_page(
        user_id=caller.user_id,
        limit=params.limit + 1,
        cursor=params.cursor,
        order=params.order,
    )
    return _thread_page(rows, limit=params.limit)


@router.get("/{thread_id}/state", response_model=ThreadStateResponse)
async def get_state(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    checkpoint_id: Annotated[str | None, Query(max_length=128, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")] = None,
) -> ThreadStateResponse:
    await require_visible_thread(services, thread_id, caller)
    return await read_thread_state(services, thread_id, checkpoint_id=checkpoint_id)


@router.get("/{thread_id}/messages", response_model=Page[MessageResponse])
async def list_messages(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    params: Annotated[MessagePageParams, Depends(message_page_params)],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> Page[MessageResponse]:
    await require_visible_thread(services, thread_id, caller)
    rows = await services.event_store.list_messages(
        thread_id,
        limit=params.limit + 1,
        before_seq=params.before_seq,
        after_seq=params.after_seq,
    )
    page, has_more = trim_message_page(rows, limit=params.limit, after_seq=params.after_seq)
    wired = [message_to_wire(row) for row in page]
    next_cursor = str(wired[-1].seq) if has_more and wired else None
    return Page(data=wired, has_more=has_more, next_cursor=next_cursor)


@router.post("/{thread_id}/state", response_model=ThreadStateResponse)
async def update_thread_state(
    thread_id: ThreadId,
    body: ThreadStateUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> ThreadStateResponse:
    await require_visible_thread(services, thread_id, caller)
    checkpoint_id = _checkpoint_id_from_body(body)
    async with reserve_checkpoint_write(services, thread_id, user_id=caller.user_id):
        await apply_state_update(
            services,
            thread_id,
            caller.user_id,
            values=body.values,
            checkpoint_id=checkpoint_id,
        )
    return await read_thread_state(services, thread_id)


@router.post("/{thread_id}/compact", response_model=ThreadCompactResponse)
async def compact_thread(
    thread_id: ThreadId,
    body: ThreadCompactRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> ThreadCompactResponse:
    await require_visible_thread(services, thread_id, caller)
    async with reserve_checkpoint_write(services, thread_id, user_id=caller.user_id):
        result = await compact_thread_context(
            services,
            thread_id,
            user_id=caller.user_id,
            force=body.force,
            keep=body.keep.to_tuple() if body.keep is not None else None,
            model_name=body.model_name,
            raise_on_failure=True,
        )
    if result is None:
        return ThreadCompactResponse(thread_id=thread_id, compacted=False, reason="nothing_to_compact")
    return ThreadCompactResponse(
        thread_id=thread_id,
        compacted=True,
        removed_message_count=result.removed_message_count,
        preserved_message_count=result.preserved_message_count,
        summary_updated=True,
        checkpoint_id=result.checkpoint_id,
        total_tokens=result.total_tokens,
    )


@router.get("/{thread_id}/history", response_model=Page[HistoryEntry])
async def get_thread_history(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=128)] = None,
) -> Page[HistoryEntry]:
    await require_visible_thread(services, thread_id, caller)
    rows = await walk_history(services, thread_id, limit=limit + 1, before=before)
    page, has_more = rows[:limit], len(rows) > limit
    next_cursor = page[-1].checkpoint_id if has_more and page else None
    return Page(data=page, has_more=has_more, next_cursor=next_cursor)


@router.get("/{thread_id}/branches", response_model=BranchListResponse)
async def list_thread_branches(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> BranchListResponse:
    await require_visible_thread(services, thread_id, caller)
    return BranchListResponse(branches=await list_branches(services, thread_id))


@router.get("/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> ThreadResponse:
    row = await store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
    return thread_to_wire(row)


@router.patch("/{thread_id}", response_model=ThreadResponse)
async def patch_thread(
    thread_id: ThreadId,
    body: ThreadPatchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> ThreadResponse:
    row = await store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
    if body.metadata is not None:
        await store.update_metadata(thread_id, body.metadata, user_id=_owner(caller))
        row = await store.get(thread_id, user_id=caller.user_id)
    return thread_to_wire(row)


@router.delete("/{thread_id}", response_model=DeletedResponse)
async def delete_thread_route(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:delete"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> DeletedResponse:
    await delete_thread(services, thread_id, caller)
    return DeletedResponse(deleted=thread_id)
