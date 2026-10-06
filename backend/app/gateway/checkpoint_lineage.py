"""Checkpoint lineage for history, branches, and manual state edits."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langgraph.types import Overwrite

from agentkit.agents.thread_state import THREAD_STATE_REDUCER_FIELDS, get_thread_state_schema
from agentkit.persistence.base import InvalidCursor, iso_utc
from agentkit.runtime.checkpoint_state import build_thread_checkpoint_state_accessor
from agentkit.runtime.runs.worker import build_state_mutation_graph
from backend.app.gateway.deps import RunServices
from backend.app.gateway.errors import InvalidStateUpdate
from backend.app.gateway.models.response import BranchPoint, HistoryEntry
from backend.app.gateway.services import CheckpointNotFound, strip_server_owned_message_keys

_HIDDEN_PREFIX = "__pregel_"
_LINEAGE_KEYS = {"checkpoint_id", "parent_checkpoint_id", "created_at", "next", "checkpoint_metadata"}
_WRITABLE = THREAD_STATE_REDUCER_FIELDS | {"thread_data", "title", "uploaded_files", "summary_text"}


def _graph(services: RunServices, node_name: str):
    return build_state_mutation_graph(
        node_name,
        checkpointer=services.run_context.checkpointer,
        state_schema=get_thread_state_schema(),
    )


def _accessor(services: RunServices, thread_id: str):
    checkpointer = services.run_context.checkpointer
    graph = _graph(services, "history_read")
    return build_thread_checkpoint_state_accessor(thread_id, graph=graph, checkpointer=checkpointer)


def _stamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        return iso_utc(value)
    if value in (None, ""):
        return None
    return str(value)


def _history_entry(state: dict[str, Any], *, include_messages: bool) -> HistoryEntry:
    values = {
        key: value
        for key, value in state.items()
        if key not in _LINEAGE_KEYS and not str(key).startswith(_HIDDEN_PREFIX)
    }
    if not include_messages:
        values.pop("messages", None)
    next_nodes = state.get("next") or ()
    return HistoryEntry(
        checkpoint_id=str(state.get("checkpoint_id") or ""),
        parent_checkpoint_id=state.get("parent_checkpoint_id"),
        created_at=_stamp(state.get("created_at")),
        next=[str(node) for node in next_nodes],
        metadata=dict(state.get("checkpoint_metadata") or {}),
        values=values,
    )


async def walk_history(services: RunServices, thread_id: str, *, limit: int, before: str | None) -> list[HistoryEntry]:
    """Settled checkpoints from the head backward. ``before`` is the previous page's last id."""
    rows: list[HistoryEntry] = []
    passed_cursor = before is None
    async for state in _accessor(services, thread_id).history(None):
        checkpoint_id = state.get("checkpoint_id")
        if not passed_cursor:
            if checkpoint_id == before:
                passed_cursor = True
            continue
        rows.append(_history_entry(state, include_messages=not rows))
        if len(rows) >= limit:
            break
    if before is not None and not passed_cursor:
        raise InvalidCursor("cursor is not on this thread lineage")
    return rows


async def list_branches(services: RunServices, thread_id: str) -> list[BranchPoint]:
    """Settled siblings off the current head's ancestor chain.

    ``history()`` only walks that chain, so a linear thread has no branches.
    """
    ancestors: set[str] = set()
    async for state in _accessor(services, thread_id).history(None):
        checkpoint_id = state.get("checkpoint_id")
        if checkpoint_id:
            ancestors.add(str(checkpoint_id))
    del ancestors
    return []


def _checkpoint_id_from_body(body) -> str | None:
    checkpoint = body.checkpoint if isinstance(body.checkpoint, dict) else None
    if checkpoint and checkpoint.get("checkpoint_id"):
        return str(checkpoint["checkpoint_id"])
    return body.checkpoint_id


async def apply_state_update(
    services: RunServices,
    thread_id: str,
    user_id: str | None,
    *,
    values: dict[str, Any] | None,
    checkpoint_id: str | None,
) -> None:
    del user_id
    if not values:
        return
    unknown = sorted(set(values) - _WRITABLE)
    if unknown:
        raise InvalidStateUpdate(f"Unknown state channel: {unknown[0]}")
    if checkpoint_id is not None:
        found = await _accessor(services, thread_id).get_state(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
        )
        if found.get("checkpoint_id") != checkpoint_id:
            raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
    payload = dict(values)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                strip_server_owned_message_keys(message, internal=False)
        payload["messages"] = Overwrite(messages)
    for key in THREAD_STATE_REDUCER_FIELDS - {"messages"}:
        if key in payload:
            payload[key] = Overwrite(payload[key])
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    graph = _graph(services, "manual_state_update")
    await graph.aupdate_state(config, payload, as_node="manual_state_update")
