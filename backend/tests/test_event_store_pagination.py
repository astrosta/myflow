"""RunEventStore.list_events limit contract for the memory and jsonl backends."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentkit.persistence.events.jsonl import JsonlRunEventStore
from agentkit.persistence.events.memory import MemoryRunEventStore

THREAD_ID = "thread-1"
RUN_ID = "run-1"


def _event(index: int) -> dict:
    return {
        "thread_id": THREAD_ID,
        "run_id": RUN_ID,
        "event_type": "run.start",
        "category": "trace",
        "content": str(index),
        "metadata": {"index": index},
    }


async def _assert_limit_contract(store) -> None:
    await store.put_batch([_event(index) for index in range(1, 6)])

    first = await store.list_events(THREAD_ID, RUN_ID, limit=2)
    assert [row["seq"] for row in first] == [1, 2]
    assert [row["content"] for row in first] == ["1", "2"]

    second = await store.list_events(THREAD_ID, RUN_ID, after_seq=2, limit=2)
    assert [row["seq"] for row in second] == [3, 4]
    assert [row["content"] for row in second] == ["3", "4"]

    # Existing callers omit limit and keep receiving the whole filtered run.
    everything = await store.list_events(THREAD_ID, RUN_ID)
    assert [row["seq"] for row in everything] == [1, 2, 3, 4, 5]


def test_memory_store_limit_and_after_seq() -> None:
    asyncio.run(_assert_limit_contract(MemoryRunEventStore()))


def test_jsonl_store_limit_and_after_seq(tmp_path: Path) -> None:
    asyncio.run(_assert_limit_contract(JsonlRunEventStore(str(tmp_path / "events"))))
