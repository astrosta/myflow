"""Admin status/flush surface for the OpenViking memory runtime."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from agentkit.agents.memory.openviking.runtime import get_openviking_memory_runtime
from backend.app.gateway.deps import require_scope
from backend.app.gateway.errors import MemoryRuntimeUnavailable
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/admin/memory", tags=["openviking-memory-admin"])


class MemoryStatusResponse(BaseModel):
    healthy: bool
    degraded: bool
    pending_stream_messages: int
    pending_missing_turn_ends: int
    last_error: str | None = None
    open_sessions: int


class FlushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None


class FlushResponse(BaseModel):
    flushed: int


def _runtime():
    runtime = get_openviking_memory_runtime()
    if runtime is None:
        raise MemoryRuntimeUnavailable("OpenViking memory runtime is not initialized")
    return runtime


@router.get("/status", response_model=MemoryStatusResponse)
async def memory_status(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> MemoryStatusResponse:
    del caller
    runtime = _runtime()
    status = await runtime.facade.status()
    return MemoryStatusResponse(
        healthy=status.healthy,
        degraded=status.degraded,
        pending_stream_messages=status.pending_stream_messages,
        pending_missing_turn_ends=status.pending_missing_turn_ends,
        last_error=status.last_error,
        open_sessions=await runtime.facade.open_session_count(),
    )


@router.post("/flush", response_model=FlushResponse)
async def flush_memory(
    body: FlushRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> FlushResponse:
    del caller
    runtime = _runtime()
    return FlushResponse(flushed=await runtime.facade.flush(user_id=body.user_id))
