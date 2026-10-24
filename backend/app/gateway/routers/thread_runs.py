"""Run create, stream, wait, join, and cancel."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from agentkit.runtime.runs import CancelOutcome, RunStatus
from backend.app.gateway.deps import RunServices, get_run_services, require_scope
from backend.app.gateway.idempotency import parse_idempotency_key
from backend.app.gateway.models.request import JsonScalar, RunId, ThreadId
from backend.app.gateway.models.response import (
    CancelResponse,
    Page,
    RunResponse,
    RunStateResponse,
    run_to_wire,
)
from backend.app.gateway.pagination import PageParams, metadata_filters, page_params, page_response
from backend.app.gateway.run_models import RunCaller, RunCancelRequest, RunCreateRequest, RunLaunch
from backend.app.gateway.thread_access import require_visible_thread
from backend.app.gateway.services import (
    RunNotCancellable,
    RunNotFound,
    RunNotLocal,
    RunRequestInvalid,
    StartRunError,
    _require_cancel_capability,
    sse_response,
    start_run,
    wait_for_run_completion,
)
from backend.app.gateway.state_reader import read_run_terminal_state

router = APIRouter(prefix="/threads", tags=["runs"])


@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> StreamingResponse:
    launch = await start_run(
        body,
        thread_id=thread_id,
        caller=caller,
        services=services,
        idempotency_key=idempotency_key,
    )
    return sse_response(services, launch, request)


@router.post("/{thread_id}/runs/wait", response_model=RunStateResponse)
async def wait_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> RunStateResponse:
    launch = await start_run(
        body,
        thread_id=thread_id,
        caller=caller,
        services=services,
        idempotency_key=idempotency_key,
    )
    final = await wait_for_run_completion(services, launch.record, request)
    return await read_run_terminal_state(services, final)


@router.post("/{thread_id}/runs", status_code=202, response_model=RunResponse)
async def create_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> RunResponse:
    launch = await start_run(
        body,
        thread_id=thread_id,
        caller=caller,
        services=services,
        idempotency_key=idempotency_key,
    )
    return run_to_wire(launch.record)


_RUN_STATUSES = {status.value for status in RunStatus}


def parse_status_filter(
    status: Annotated[str | None, Query(max_length=128)] = None,
) -> frozenset[RunStatus] | None:
    if status is None or status == "":
        return None
    names = [part.strip() for part in status.split(",") if part.strip()]
    unknown = [name for name in names if name not in _RUN_STATUSES]
    if unknown or not names:
        raise RunRequestInvalid(f"Unknown run status: {', '.join(unknown) or status}")
    return frozenset(RunStatus(name) for name in names)


@router.get("/{thread_id}/runs", response_model=Page[RunResponse])
async def list_runs(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("runs:read"))],
    params: Annotated[PageParams, Depends(page_params)],
    statuses: Annotated[frozenset[RunStatus] | None, Depends(parse_status_filter)],
    metadata: Annotated[dict[str, JsonScalar], Depends(metadata_filters)],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> Page[RunResponse]:
    await require_visible_thread(services, thread_id, caller)
    rows = await services.run_store.list_by_thread(
        thread_id,
        user_id=caller.user_id,
        limit=params.limit + 1,
        before=params.cursor,
        order=params.order,
        statuses=statuses,
        metadata=metadata or None,
    )
    page, has_more = rows[: params.limit], len(rows) > params.limit
    wired = [run_to_wire(row) for row in page]
    next_cursor = page[-1]["cursor"] if has_more and page else None
    return page_response(wired, has_more=has_more, next_cursor=next_cursor)


@router.get("/{thread_id}/runs/{run_id}", response_model=RunResponse)
async def get_run(
    thread_id: ThreadId,
    run_id: RunId,
    caller: Annotated[RunCaller, Depends(require_scope("runs:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> RunResponse:
    record = await services.run_manager.get(run_id, user_id=caller.user_id)
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    return run_to_wire(record)


@router.get("/{thread_id}/runs/{run_id}/join")
async def join_run(
    thread_id: ThreadId,
    run_id: RunId,
    request: Request,
    caller: RunCaller = Depends(require_scope("runs:read")),
) -> StreamingResponse:
    services = get_run_services(request)
    record = await services.run_manager.get(run_id, user_id=caller.user_id)
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    if record.store_only and not services.bridge.supports_cross_process:
        raise RunNotLocal(f"Run {run_id} is owned by another process")
    return sse_response(services, RunLaunch(record=record, reused=True), request, apply_on_disconnect=False)


_OUTCOME_ERRORS: dict[CancelOutcome, type[StartRunError]] = {
    CancelOutcome.unknown: RunNotFound,
    CancelOutcome.not_active_locally: RunNotLocal,
    CancelOutcome.not_cancellable: RunNotCancellable,
}


@router.post("/{thread_id}/runs/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(
    thread_id: ThreadId,
    run_id: RunId,
    request: Request,
    body: RunCancelRequest | None = None,
    caller: RunCaller = Depends(require_scope("runs:cancel")),
) -> dict[str, Any]:
    services = get_run_services(request)
    req = body or RunCancelRequest()
    _require_cancel_capability(caller, req.action)
    record = await services.run_manager.get(run_id, user_id=caller.user_id)
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    outcome = await services.run_manager.cancel(run_id, action=req.action)
    if (error := _OUTCOME_ERRORS.get(outcome)) is not None:
        raise error(f"Run {run_id}: {outcome.value}")
    if req.wait:
        final = await wait_for_run_completion(services, record, request, apply_on_disconnect=False)
        return {"outcome": outcome.value, "run": run_to_wire(final)}
    return {"outcome": outcome.value}
