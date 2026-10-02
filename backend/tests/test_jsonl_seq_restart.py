"""§5.5 jsonl seq survives a new store on the same directory."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentkit.runtime.events.catalog import LLM_HUMAN_INPUT
from agentkit.runtime.events.store import JsonlRunEventStore


def _event(run_id: str, content: str) -> dict:
    return {
        "thread_id": "thread-1",
        "run_id": run_id,
        "event_type": LLM_HUMAN_INPUT.event_type,
        "category": LLM_HUMAN_INPUT.category,
        "content": content,
        "metadata": {},
    }


def test_restarted_jsonl_store_continues_from_max_seq(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = JsonlRunEventStore(tmp_path)
        written = await first.put_batch([_event("run-a", "one"), _event("run-a", "two")])
        assert [row["seq"] for row in written] == [1, 2]

        restarted = JsonlRunEventStore(tmp_path)
        continued = await restarted.put_batch([_event("run-b", "three")])
        assert continued[0]["seq"] == 3
        assert [row["seq"] for row in await restarted.list_messages("thread-1", limit=10)] == [1, 2, 3]
        assert [row["content"] for row in await restarted.list_events("thread-1", "run-a")] == ["one", "two"]

    asyncio.run(scenario())
