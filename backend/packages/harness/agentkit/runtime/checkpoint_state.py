"""Read checkpoint state through the compiled graph, never through a time sort."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

from agentkit.runtime.events.catalog import LLM_AI_RESPONSE, LLM_HUMAN_INPUT, LLM_TOOL_RESULT
from agentkit.persistence.events.base import RunEventStore

CHECKPOINT_SEED_RUN_ID = "checkpoint-seed"
ChannelMode = Literal["full", "delta"]


class CheckpointChannelModeConflict(RuntimeError):
    """A full reader opened a delta checkpoint, or M4 was asked to read delta. Maps to HTTP 409."""


class CheckpointStateAccessor:
    """Binds one graph, its checkpointer, and the mode frozen at process start."""

    def __init__(
        self,
        graph: Any,
        checkpointer: Any,
        *,
        mode: ChannelMode = "full",
        thread_id: str | None = None,
    ) -> None:
        if mode not in ("full", "delta"):
            raise ValueError(f"checkpoint_channel_mode must be full or delta, got {mode!r}")
        if getattr(graph, "checkpointer", None) is not checkpointer:
            raise ValueError("graph checkpointer and accessor checkpointer must be the same instance")
        self._graph = graph
        self._checkpointer = checkpointer
        self._mode = mode
        self._thread_id = thread_id

    async def get_state(self, config: dict | None) -> dict[str, Any]:
        """Head checkpoint for this thread. Messages are readable; ``next`` is returned as stored."""
        self._require_full_reader()
        snapshot = await self._graph.aget_state(self._resolve(config))
        if _checkpoint_id(getattr(snapshot, "config", None)) is None:
            return {"messages": [], "next": ()}
        _reject_delta_checkpoint(snapshot)
        return _public_state(snapshot)

    async def history(
        self,
        config: dict | None,
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk ``parent_config`` and keep only settled checkpoints (``next`` is empty)."""
        self._require_full_reader()
        yielded = 0
        async for snapshot in self._lineage(config):
            yield _public_state(snapshot)
            yielded += 1
            if limit is not None and yielded >= limit:
                return

    def _require_full_reader(self) -> None:
        if self._mode == "delta":
            raise CheckpointChannelModeConflict(
                "checkpoint_channel_mode delta is not implemented; refusing to read sentinel messages"
            )

    def _resolve(self, config: dict | None) -> dict:
        if config is not None:
            return config
        if not self._thread_id:
            raise ValueError("checkpoint read requires a config or a bound thread_id")
        return {"configurable": {"thread_id": self._thread_id, "checkpoint_ns": ""}}

    async def _lineage(self, config: dict | None) -> AsyncIterator[Any]:
        current: dict | None = self._resolve(config)
        seen: set[str] = set()
        while current is not None:
            snapshot = await self._graph.aget_state(current)
            checkpoint_id = _checkpoint_id(getattr(snapshot, "config", None))
            if checkpoint_id is None or checkpoint_id in seen:
                return
            seen.add(checkpoint_id)
            _reject_delta_checkpoint(snapshot)
            # A paused graph normally is not a settled history entry. HITL is
            # the exception: the pending interrupt is exactly what the client
            # needs to render its confirmation panel after a refresh.
            if not _has_pending_tasks(snapshot) or _pending_interrupts(snapshot):
                yield snapshot
            current = getattr(snapshot, "parent_config", None)


def build_thread_checkpoint_state_accessor(
    thread_id: str,
    *,
    graph: Any,
    checkpointer: Any,
    mode: ChannelMode = "full",
) -> CheckpointStateAccessor:
    """The thread-state route binds the process graph once per read."""
    return CheckpointStateAccessor(graph, checkpointer, mode=mode, thread_id=thread_id)


async def ensure_checkpoint_history_seeded(
    thread_id: str,
    store: RunEventStore,
    accessor: CheckpointStateAccessor,
) -> None:
    """Copy checkpoint messages into an empty ledger. A second call does nothing."""
    if await store.count_messages(thread_id) > 0:
        return
    state = await accessor.get_state(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    messages = state.get("messages") or []
    if not messages:
        return
    await store.put_batch(messages_to_events(thread_id, messages))


def messages_to_events(thread_id: str, messages: list[Any]) -> list[dict[str, Any]]:
    """One ledger row per checkpoint message. The seed run id is stable so a retry stays one batch."""
    events = []
    for message in messages:
        definition = _definition_for(message)
        events.append(
            {
                "thread_id": thread_id,
                "run_id": CHECKPOINT_SEED_RUN_ID,
                "user_id": None,
                "event_type": definition.event_type,
                "category": definition.category,
                "content": _message_content(message),
                "metadata": {},
            }
        )
    return events


def _definition_for(message: Any):
    kind = _message_kind(message)
    if kind in {"human", "user"}:
        return LLM_HUMAN_INPUT
    if kind == "tool":
        return LLM_TOOL_RESULT
    return LLM_AI_RESPONSE


def _message_kind(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("type") or message.get("role") or "ai")
    return str(getattr(message, "type", None) or "ai")


def _message_content(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("content", "")
    content = getattr(message, "content", None)
    return "" if content is None else content


def _checkpoint_id(config: Any) -> str | None:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable") or {}
    checkpoint_id = configurable.get("checkpoint_id")
    if not checkpoint_id:
        return None
    return str(checkpoint_id)


def _reject_delta_checkpoint(snapshot: Any) -> None:
    metadata = getattr(snapshot, "metadata", None) or {}
    stored = metadata.get("checkpoint_channel_mode") or "full"
    if stored == "delta":
        raise CheckpointChannelModeConflict(
            "checkpoint_channel_mode is delta; a full reader returns 409 instead of empty messages"
        )


def _has_pending_tasks(snapshot: Any) -> bool:
    """The graph has not paused. ``next`` names the nodes still queued for this checkpoint."""
    return bool(tuple(getattr(snapshot, "next", ()) or ()))


def _pending_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    """Flatten task interrupts into the shape the SDK exposes on values."""

    out: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", None) or ():
        for item in getattr(task, "interrupts", None) or ():
            out.append({"id": getattr(item, "id", None), "value": getattr(item, "value", None)})
    return out


def _public_state(snapshot: Any) -> dict[str, Any]:
    values = dict(getattr(snapshot, "values", None) or {})
    interrupts = _pending_interrupts(snapshot)
    if interrupts:
        values["__interrupt__"] = interrupts
    configurable = (getattr(snapshot, "config", None) or {}).get("configurable") or {}
    parent = getattr(snapshot, "parent_config", None) or {}
    parent_configurable = parent.get("configurable") or {} if isinstance(parent, Mapping) else {}
    values["checkpoint_id"] = configurable.get("checkpoint_id")
    values["parent_checkpoint_id"] = parent_configurable.get("checkpoint_id")
    values["created_at"] = getattr(snapshot, "created_at", None)
    values["next"] = tuple(getattr(snapshot, "next", ()) or ())
    metadata = getattr(snapshot, "metadata", None) or {}
    values["checkpoint_metadata"] = dict(metadata)
    return values
