"""Checkpoint snapshots for GET /state and POST /wait. Routes do not touch the checkpointer."""

from __future__ import annotations

from typing import Any

from backend.app.gateway.deps import RunServices
from backend.app.gateway.models.response import RunStateResponse, ThreadStateResponse
from backend.app.gateway.services import CheckpointNotFound
from agentkit.runtime.runs import RunRecord, RunStatus

_HIDDEN_PREFIX = "__pregel_"


def _pending_interrupts(saved: Any) -> list[dict[str, Any]]:
    """Read interrupts from checkpoint pending writes without rebuilding the graph."""

    out: list[dict[str, Any]] = []
    for write in getattr(saved, "pending_writes", None) or ():
        if not isinstance(write, (tuple, list)) or len(write) < 3:
            continue
        if write[1] != "__interrupt__":
            continue
        for item in write[2] or ():
            out.append({"id": getattr(item, "id", None), "value": getattr(item, "value", None)})
    return out


async def read_thread_state(
    services: RunServices, thread_id: str, *, checkpoint_id: str | None = None
) -> ThreadStateResponse:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    saved = await services.run_context.checkpointer.aget_tuple(config)
    if saved is None:
        if checkpoint_id is not None:
            raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
        return ThreadStateResponse(values={}, checkpoint_id=None, checkpoint_ns="")
    checkpoint = getattr(saved, "checkpoint", None) or {}
    channels = checkpoint.get("channel_values") or {}
    values = {
        key: value
        for key, value in dict(channels).items()
        if not str(key).startswith(_HIDDEN_PREFIX)
    }
    interrupts = _pending_interrupts(saved)
    if interrupts:
        values["__interrupt__"] = interrupts
    configurable = (getattr(saved, "config", None) or {}).get("configurable") or {}
    return ThreadStateResponse(
        values=values,
        checkpoint_id=configurable.get("checkpoint_id"),
        checkpoint_ns=configurable.get("checkpoint_ns") or "",
    )


def _status_value(status: RunStatus | str) -> str:
    return status.value if isinstance(status, RunStatus) else str(status)


async def read_run_terminal_state(services: RunServices, record: RunRecord) -> RunStateResponse:
    """Coordinates come from this run's metadata, not from the thread head."""
    checkpoint_id = (record.metadata or {}).get("checkpoint_id")
    status = _status_value(record.status)
    if not isinstance(checkpoint_id, str):
        return RunStateResponse(
            values={},
            checkpoint_id=None,
            checkpoint_ns="",
            run_id=record.run_id,
            status=status,
            error=record.error,
        )
    state = await read_thread_state(services, record.thread_id, checkpoint_id=checkpoint_id)
    return RunStateResponse(
        values=state.values,
        checkpoint_id=state.checkpoint_id,
        checkpoint_ns=state.checkpoint_ns,
        run_id=record.run_id,
        status=status,
        error=record.error,
    )
