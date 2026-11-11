"""Checkpoint snapshots for GET /state and POST /wait. Routes do not touch the checkpointer."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentkit.runtime.runs import RunRecord, RunStatus
from agentkit.sandbox.artifacts import list_output_artifacts
from backend.app.gateway.deps import RunServices
from backend.app.gateway.models.response import RunStateResponse, ThreadStateResponse
from backend.app.gateway.services import CheckpointNotFound

logger = logging.getLogger(__name__)

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


async def _scanned_artifacts(thread_id: str, user_id: str | None) -> list[str] | None:
    """Read the filesystem truth; return None when scanning must not hide state."""

    try:
        return await asyncio.to_thread(list_output_artifacts, thread_id, user_id=user_id)
    except Exception:
        logger.warning("Artifact scan failed for thread %s", thread_id, exc_info=True)
        return None


def _with_scanned_artifacts(values: dict[str, Any], artifacts: list[str] | None) -> dict[str, Any]:
    """Prefer files that exist now, but do not add an empty key to old state."""

    if artifacts is None:
        return values
    if artifacts or "artifacts" in values:
        values["artifacts"] = artifacts
    return values


async def read_thread_state(
    services: RunServices,
    thread_id: str,
    *,
    checkpoint_id: str | None = None,
    user_id: str | None = None,
) -> ThreadStateResponse:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    saved = await services.run_context.checkpointer.aget_tuple(config)
    if saved is None and checkpoint_id is not None:
        raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
    artifacts = await _scanned_artifacts(thread_id, user_id)
    if saved is None:
        values = _with_scanned_artifacts({}, artifacts)
        return ThreadStateResponse(values=values, checkpoint_id=None, checkpoint_ns="")
    checkpoint = getattr(saved, "checkpoint", None) or {}
    channels = checkpoint.get("channel_values") or {}
    values = {
        key: value
        for key, value in dict(channels).items()
        if not str(key).startswith(_HIDDEN_PREFIX)
    }
    values = _with_scanned_artifacts(values, artifacts)
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
    state = await read_thread_state(
        services, record.thread_id, checkpoint_id=checkpoint_id, user_id=record.user_id
    )
    return RunStateResponse(
        values=state.values,
        checkpoint_id=state.checkpoint_id,
        checkpoint_ns=state.checkpoint_ns,
        run_id=record.run_id,
        status=status,
        error=record.error,
    )
