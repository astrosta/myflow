"""§6 checkpoint reads follow the parent chain, and an empty ledger is seeded once."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from agentkit.runtime.checkpoint_state import (
    CheckpointChannelModeConflict,
    CheckpointStateAccessor,
    ensure_checkpoint_history_seeded,
    messages_to_events,
)
from agentkit.runtime.events.catalog import LLM_AI_RESPONSE, LLM_HUMAN_INPUT
from agentkit.persistence.events import JsonlRunEventStore, MemoryRunEventStore


class _Snapshot:
    def __init__(
        self,
        checkpoint_id: str,
        values: dict,
        parent_id: str | None,
        *,
        created_at: str,
        metadata: dict | None = None,
        next_: tuple = (),
        tasks: tuple = (),
    ) -> None:
        self.values = values
        self.next = next_
        self.tasks = tasks
        self.metadata = metadata or {}
        self.created_at = created_at
        self.config = {
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }
        self.parent_config = None
        if parent_id is not None:
            self.parent_config = {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                    "checkpoint_id": parent_id,
                }
            }


class _Graph:
    def __init__(self, snapshots: list[_Snapshot], *, head: str, checkpointer: object) -> None:
        self.checkpointer = checkpointer
        self._by_id = {item.config["configurable"]["checkpoint_id"]: item for item in snapshots}
        self._head = head

    async def aget_state(self, config):
        configurable = (config or {}).get("configurable") or {}
        checkpoint_id = configurable.get("checkpoint_id") or self._head
        return self._by_id[checkpoint_id]


def _chain() -> tuple[object, _Graph, dict, dict]:
    """Main leaf C is older than sibling S. A time sort would put S first."""
    saver = object()
    snapshots = [
        _Snapshot("A", {"messages": ["a"]}, None, created_at="2026-01-01T00:00:00Z"),
        _Snapshot(
            "B",
            {"messages": ["a", "b"]},
            "A",
            created_at="2026-01-02T00:00:00Z",
            tasks=("finished",),
        ),
        _Snapshot(
            "P",
            {"messages": ["partial"]},
            "B",
            created_at="2026-01-03T00:00:00Z",
            next_=("step",),
        ),
        _Snapshot("C", {"messages": ["a", "b", "c"]}, "P", created_at="2026-01-04T00:00:00Z"),
        _Snapshot(
            "H",
            {"messages": ["a", "b", "c", "user"]},
            "C",
            created_at="2026-01-04T00:00:01Z",
            next_=("model",),
        ),
        _Snapshot("S", {"messages": ["a", "b", "sibling"]}, "B", created_at="2026-01-05T00:00:00Z"),
    ]
    graph = _Graph(snapshots, head="H", checkpointer=saver)
    main = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "C"}}
    sibling = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "S"}}
    return saver, graph, main, sibling


def test_history_follows_parents_and_skips_pending_snapshots() -> None:
    async def scenario() -> None:
        saver, graph, main, sibling = _chain()
        accessor = CheckpointStateAccessor(graph, saver, mode="full", thread_id="thread-1")
        main_line = [item async for item in accessor.history(main)]
        assert [item["messages"] for item in main_line] == [["a", "b", "c"], ["a", "b"], ["a"]]
        assert [item["created_at"] for item in main_line] == [
            "2026-01-04T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ]
        sibling_line = [item async for item in accessor.history(sibling, limit=2)]
        assert [item["messages"] for item in sibling_line] == [["a", "b", "sibling"], ["a", "b"]]
        assert [item["next"] for item in main_line] == [(), (), ()]
        state = await accessor.get_state(None)
        assert state["messages"] == ["a", "b", "c", "user"]
        assert state["next"] == ("model",)
        assert state["checkpoint_id"] == "H"
        replay = [item async for item in accessor.history(None)]
        assert [item["checkpoint_id"] for item in replay] == ["C", "B", "A"]

    asyncio.run(scenario())


def test_full_reader_rejects_a_delta_checkpoint() -> None:
    async def scenario() -> None:
        saver = object()
        marked = _Snapshot(
            "D",
            {"messages": []},
            None,
            created_at="2026-01-01T00:00:00Z",
            metadata={"checkpoint_channel_mode": "delta"},
        )
        plain = _Snapshot("F", {"messages": ["kept"]}, None, created_at="2026-01-01T00:00:00Z")
        graph = _Graph([marked, plain], head="F", checkpointer=saver)
        accessor = CheckpointStateAccessor(graph, saver, mode="full", thread_id="thread-1")
        with pytest.raises(CheckpointChannelModeConflict, match="delta"):
            await accessor.get_state(
                {"configurable": {"thread_id": "thread-1", "checkpoint_id": "D"}}
            )
        state = await accessor.get_state(None)
        assert state["messages"] == ["kept"]
        delta_reader = CheckpointStateAccessor(graph, saver, mode="delta", thread_id="thread-1")
        with pytest.raises(CheckpointChannelModeConflict, match="delta"):
            await delta_reader.get_state(None)

    asyncio.run(scenario())


def test_accessor_requires_the_graph_checkpointer_instance() -> None:
    saver, graph, _main, _sibling = _chain()
    with pytest.raises(ValueError, match="checkpointer"):
        CheckpointStateAccessor(graph, object(), mode="full", thread_id="thread-1")
    CheckpointStateAccessor(graph, saver, mode="full", thread_id="thread-1")


def test_empty_ledger_is_seeded_once_from_checkpoint_messages() -> None:
    async def scenario() -> None:
        saver = object()
        snapshot = _Snapshot(
            "A",
            {"messages": [{"type": "human", "content": "你好"}, {"type": "ai", "content": "好"}]},
            None,
            created_at="2026-01-01T00:00:00Z",
        )
        graph = _Graph([snapshot], head="A", checkpointer=saver)
        accessor = CheckpointStateAccessor(graph, saver, mode="full", thread_id="thread-1")
        store = MemoryRunEventStore()
        await ensure_checkpoint_history_seeded("thread-1", store, accessor)
        await ensure_checkpoint_history_seeded("thread-1", store, accessor)
        rows = await store.list_messages("thread-1", limit=10)
        assert [row["content"] for row in rows] == ["你好", "好"]
        assert [row["event_type"] for row in rows] == [
            LLM_HUMAN_INPUT.event_type,
            LLM_AI_RESPONSE.event_type,
        ]
        assert await store.count_messages("thread-1") == 2

        occupied = MemoryRunEventStore()
        await occupied.put_batch(messages_to_events("thread-1", [{"type": "human", "content": "already"}]))
        await ensure_checkpoint_history_seeded("thread-1", occupied, accessor)
        kept = await occupied.list_messages("thread-1", limit=10)
        assert [row["content"] for row in kept] == ["already"]

    asyncio.run(scenario())


def test_jsonl_count_messages_ignores_trace_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = JsonlRunEventStore(tmp_path)
        await store.put_batch(
            [
                {
                    "thread_id": "thread-1",
                    "run_id": "run-1",
                    "event_type": "run.start",
                    "category": "trace",
                    "content": "start",
                    "metadata": {},
                },
                *messages_to_events("thread-1", [{"type": "human", "content": "one"}]),
            ]
        )
        assert await store.count_messages("thread-1") == 1

    asyncio.run(scenario())


def test_gateway_routers_do_not_read_checkpoint_tuples() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "gateway" / "routers"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"get_tuple", "aget_tuple"}:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
