"""§5.3 RunJournal flushes complete messages and ignores tokens."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from agentkit.runtime.events.catalog import (
    LLM_AI_RESPONSE,
    LLM_ERROR_EVENT,
    LLM_TOOL_RESULT,
    RUN_END_EVENT,
    RUN_ERROR_EVENT,
    RUN_START_EVENT,
)
from agentkit.runtime.events.journal import RunJournal
from agentkit.runtime.events.store import MemoryRunEventStore


class _Record:
    run_id = "run-1"
    thread_id = "thread-1"
    user_id = "alice"


def test_partial_batch_is_written_on_flush_and_tokens_are_not() -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        journal = RunJournal(store, _Record(), batch_size=20, max_trace_content=4)
        run_id = uuid4()
        await journal.on_chain_start({"name": "lead"}, {"text": "中文中文"}, run_id=run_id)
        await journal.on_llm_new_token("tok", run_id=run_id)
        assert await store.list_events("thread-1", "run-1") == []
        await journal.flush()
        stored = await store.list_events("thread-1", "run-1")
        assert len(stored) == 1
        assert stored[0]["event_type"] == RUN_START_EVENT.event_type
        assert stored[0]["user_id"] == "alice"
        assert stored[0]["metadata"]["content_truncated"] is True
        assert len(stored[0]["content"].encode("utf-8")) <= 4

        await journal.on_tool_end("中文中文中文", run_id=run_id)
        await journal.flush()
        messages = await store.list_messages("thread-1", limit=10)
        assert messages[-1]["event_type"] == LLM_TOOL_RESULT.event_type
        assert messages[-1]["content"] == "中文中文中文"
        assert "content_truncated" not in messages[-1]["metadata"]

    asyncio.run(scenario())


def test_full_buffer_flushes_without_an_explicit_call() -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        journal = RunJournal(store, _Record(), batch_size=2)
        run_id = uuid4()
        await journal.on_chain_start({}, {}, run_id=run_id)
        assert await store.list_events("thread-1", "run-1") == []
        await journal.on_chain_end({"ok": True}, run_id=run_id)
        stored = await store.list_events("thread-1", "run-1")
        assert [row["event_type"] for row in stored] == [
            RUN_START_EVENT.event_type,
            RUN_END_EVENT.event_type,
        ]
        assert stored[1]["content"] == {"ok": True}

        response = SimpleNamespace(
            generations=[[SimpleNamespace(message=SimpleNamespace(content="完整回答"))]]
        )
        await journal.on_llm_end(response, run_id=run_id)
        await journal.on_llm_error(RuntimeError("llm down"), run_id=run_id)
        await journal.on_chain_error(RuntimeError("chain down"), run_id=run_id)
        await journal.on_llm_new_token("still-not-stored", run_id=run_id)
        await journal.flush()
        kinds = [row["event_type"] for row in await store.list_events("thread-1", "run-1")]
        assert kinds == [
            RUN_START_EVENT.event_type,
            RUN_END_EVENT.event_type,
            LLM_AI_RESPONSE.event_type,
            LLM_ERROR_EVENT.event_type,
            RUN_ERROR_EVENT.event_type,
        ]
        body = await store.list_messages("thread-1", limit=10)
        assert body[0]["content"] == "完整回答"

    asyncio.run(scenario())
