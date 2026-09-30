"""
/runs、/stream、/wait、/join、/cancel
"""
from typing import Any
from fastapi import router, Request
from fastapi.responses import StreamingResponse
from backend.app.gateway.deps import get_run_services, caller_from_request
from backend.app.gateway.run_models import (
    RunLaunch,
    RunCancelRequest,
) 
from backend.app.gateway.services import (
    sse_response,
    wait_for_run_completion,
    _require_cancel_capability,
    RunNotFound,
    RunNotLocal,
    RunNotCancellable,
    StartRunError,
)
from agentkit.runtime.runs import CancelOutcome

@router.get("/{thread_id}/runs/{run_id}/join")
async def join_run(thread_id: str, run_id: str, request: Request) -> StreamingResponse:
    services = get_run_services(request)
    caller = caller_from_request(request)
    record = await services.run_manager.get(run_id, user_id=caller.user_id)
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    if record.store_only and not services.bridge.supports_cross_process:
        raise RunNotFound(f"Run {run_id} is owned by another process")

    return sse_response(services, RunLaunch(record, reused=True), request, apply_on_disconnect=False)

_OUTCOME_ERRORS: dict[CancelOutcome, type[StartRunError]] = {
    CancelOutcome.unknown: RunNotFound,                 # 404
    CancelOutcome.not_active_locally: RunNotLocal,      # 409：这个 run 在别的进程手里
    CancelOutcome.not_cancellable: RunNotCancellable,   # 409：已经是终态
}

@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(
    thread_id: str, run_id: str, request: Request, body: RunCancelRequest | None = None
) -> dict[str, Any]:
    services = get_run_services(request)
    caller = caller_from_request(request)
    req = body or RunCancelRequest()
    _require_cancel_capability(caller, req.action)

    record = await services.run_manager.get(run_id, user_id=caller.user_id)   # 属主先判（§5.1）
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")

    outcome = await services.run_manager.cancel(run_id, action=req.action)
    if (error := _OUTCOME_ERRORS.get(outcome)) is not None:
        raise error(f"Run {run_id}: {outcome.value}")
    if req.wait:                                        # 等收尾：复用 §8.3 的同一条路径
        final = await wait_for_run_completion(services, record, request, apply_on_disconnect=False)
        return {"outcome": outcome.value, "run": run_to_wire(final)}
    return {"outcome": outcome.value}