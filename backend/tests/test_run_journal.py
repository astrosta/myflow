"""§5.7 RunJournal：哪个回调写哪条事件、去重规则、缓冲与 flush。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentkit.runtime.events.catalog import (
    LLM_AI_RESPONSE,
    LLM_ERROR_EVENT,
    LLM_HUMAN_INPUT,
    LLM_TOOL_RESULT,
    RUN_END_EVENT,
    RUN_ERROR_EVENT,
    RUN_START_EVENT,
)
from agentkit.runtime.events.journal import RunJournal
from agentkit.runtime.events.store import MemoryRunEventStore

MIDDLEWARE_TAGS = ["middleware:summarize", "langgraph_node"]


class _Record:
    run_id = "run-1"
    thread_id = "thread-1"
    user_id = "alice"


def _usage(input_tokens: int = 3, output_tokens: int = 5) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _answer(text: str = "完整回答", *, message_id: str | None = None, usage: dict | None = None) -> AIMessage:
    return AIMessage(content=text, id=message_id, usage_metadata=usage)


def _response(*messages: Any) -> SimpleNamespace:
    """LLMResult 的最小形状：generations 外层一个 prompt，内层一次生成。"""
    return SimpleNamespace(
        generations=[[SimpleNamespace(message=message)] for message in messages],
        llm_output=None,
    )


def _journal(**kwargs: Any) -> tuple[MemoryRunEventStore, RunJournal]:
    store = MemoryRunEventStore()
    return store, RunJournal(store, _Record(), **kwargs)


def _kinds(rows: list[dict[str, Any]]) -> list[str]:
    return [row["event_type"] for row in rows]


def test_root_chain_start_writes_run_start_and_node_starts_do_not() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        root = uuid4()
        await journal.on_chain_start({"name": "node"}, {}, run_id=uuid4(), parent_run_id=root)
        await journal.flush()
        assert await store.list_events("thread-1", "run-1") == []

        await journal.on_chain_start({"name": "lead_agent"}, {"text": "x"}, run_id=root)
        await journal.on_chain_start({"name": "title_agent"}, {}, run_id=uuid4(), tags=["middleware:title"])
        await journal.flush()
        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RUN_START_EVENT.event_type, RUN_START_EVENT.event_type]
        assert rows[0]["content"] == {"chain": "lead_agent"}
        assert rows[0]["category"] == "trace"
        assert rows[0]["metadata"] == {"caller": "lead"}
        assert rows[1]["metadata"] == {"caller": "middleware:title"}

    asyncio.run(scenario())


def test_chat_model_start_writes_human_messages_once() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        human = HumanMessage("你好", id="h-1")
        messages = [[human, SystemMessage("sys", id="s-1"), _answer("旧回答", message_id="a-1")]]

        await journal.on_chat_model_start({"name": "model"}, messages, run_id=uuid4())
        await journal.on_chat_model_start({"name": "model"}, messages, run_id=uuid4())  # 回放
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_HUMAN_INPUT.event_type]
        assert rows[0]["content"] == human.model_dump()
        assert rows[0]["metadata"] == {"caller": "lead", "llm_call_index": 0}
        assert rows[0]["user_id"] == "alice"

    asyncio.run(scenario())


def test_middleware_tagged_model_call_skips_the_human_input() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        messages = [[HumanMessage("被压缩的历史", id="h-1")]]

        await journal.on_chat_model_start({"name": "model"}, messages, run_id=uuid4(), tags=MIDDLEWARE_TAGS)
        await journal.flush()

        assert await store.list_events("thread-1", "run-1") == []

    asyncio.run(scenario())


def test_llm_end_writes_one_row_per_generation_and_dedups_by_run_id() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        model_run = uuid4()
        await journal.on_chat_model_start({"name": "model"}, [[]], run_id=model_run)
        response = _response(
            _answer("第一段", usage=_usage()),
            _answer("第二段", usage=_usage(input_tokens=1, output_tokens=2)),
        )

        await journal.on_llm_end(response, run_id=model_run)
        await journal.on_llm_end(response, run_id=model_run)  # 流式回放：不再写第二条
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_AI_RESPONSE.event_type, LLM_AI_RESPONSE.event_type]
        assert [row["content"]["content"] for row in rows] == ["第一段", "第二段"]
        assert rows[0]["category"] == "message"
        assert rows[0]["metadata"]["usage"] == {
            "input_tokens": 3,
            "output_tokens": 5,
            "cache_read_tokens": 0,
        }
        assert rows[0]["metadata"]["llm_call_index"] == 0
        assert isinstance(rows[0]["metadata"]["latency_ms"], int)
        assert rows[1]["metadata"]["usage"]["output_tokens"] == 2

    asyncio.run(scenario())


def test_response_without_usage_is_held_until_a_later_one_supersedes_it() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        model_run = uuid4()

        await journal.on_llm_end(_response(_answer("没有 usage 的那份")), run_id=model_run)
        assert await store.list_events("thread-1", "run-1") == [], "空 usage 的那份先挂起，不写"

        await journal.on_llm_end(_response(_answer("带 usage 的那份", usage=_usage())), run_id=model_run)
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_AI_RESPONSE.event_type]
        assert rows[0]["content"]["content"] == "带 usage 的那份"
        assert rows[0]["metadata"]["usage"]["input_tokens"] == 3

    asyncio.run(scenario())


def test_held_response_is_committed_when_no_second_one_arrives() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        model_run = uuid4()

        await journal.on_llm_end(_response(_answer("缺 usage 也要留下")), run_id=model_run)
        await journal.on_llm_error(RuntimeError("next call failed"), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_AI_RESPONSE.event_type, LLM_ERROR_EVENT.event_type]
        assert rows[0]["content"]["content"] == "缺 usage 也要留下"
        assert "usage" not in rows[0]["metadata"], "没拿到 usage 就不编一个"

    asyncio.run(scenario())


def test_track_token_usage_false_keeps_usage_out_of_the_metadata() -> None:
    """§5.7：这个开关的语义是"事件里要不要带 usage"，不是"要不要统计 token"。"""

    async def scenario() -> None:
        store, journal = _journal(track_token_usage=False)
        await journal.on_llm_end(_response(_answer(usage=_usage())), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_AI_RESPONSE.event_type], "带 usage 的响应照写，只是不抄 usage"
        assert "usage" not in rows[0]["metadata"]

    asyncio.run(scenario())


def test_llm_error_shares_the_call_index_of_its_start() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        model_run = uuid4()
        await journal.on_chat_model_start({"name": "model"}, [[]], run_id=model_run)

        await journal.on_llm_error(ValueError("boom"), run_id=model_run)
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_ERROR_EVENT.event_type]
        assert rows[0]["category"] == "trace"
        assert rows[0]["content"] == "ValueError: boom"
        assert rows[0]["metadata"] == {"error_type": "ValueError", "llm_call_index": 0}

    asyncio.run(scenario())


def test_tool_end_writes_the_tool_message_and_dedups_by_call_id() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        tool_message = ToolMessage(content="42", tool_call_id="c-1", name="calc", status="success")

        await journal.on_tool_end("不是 ToolMessage：不写", run_id=uuid4())
        await journal.on_tool_end(tool_message, run_id=uuid4())
        await journal.on_tool_end(tool_message, run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type]
        assert rows[0]["content"] == tool_message.model_dump()
        assert rows[0]["metadata"] == {"tool_name": "calc", "status": "success"}

    asyncio.run(scenario())


def test_chain_end_reconciles_the_messages_the_callbacks_missed() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        outputs = {
            "messages": [
                HumanMessage("你好", id="h-1"),
                ToolMessage(content="42", tool_call_id="c-1", name="calc"),
                AIMessage("没有 id 的 AI：不补"),
                AIMessage("有 id 的 AI：补", id="a-2"),
            ]
        }

        await journal.on_chain_end(outputs, run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [
            LLM_HUMAN_INPUT.event_type,
            LLM_TOOL_RESULT.event_type,
            LLM_AI_RESPONSE.event_type,
            RUN_END_EVENT.event_type,
        ]
        assert rows[2]["content"]["content"] == "有 id 的 AI：补"
        assert rows[3]["content"] == outputs
        assert rows[3]["metadata"] == {"status": "success"}

    asyncio.run(scenario())


def test_reconcile_does_not_duplicate_an_answered_ai_message() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        model_run = uuid4()
        answer = _answer("模型自己报的", message_id="a-1", usage=_usage())

        await journal.on_llm_end(_response(answer), run_id=model_run)
        await journal.on_chain_end({"messages": [answer]}, run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_AI_RESPONSE.event_type, RUN_END_EVENT.event_type]

    asyncio.run(scenario())


def test_chain_error_flushes_by_itself() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        await journal.on_chain_start({"name": "lead_agent"}, {}, run_id=uuid4())

        await journal.on_chain_error(TimeoutError("图超时"), run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RUN_START_EVENT.event_type, RUN_ERROR_EVENT.event_type]
        assert rows[1]["content"] == "TimeoutError: 图超时"
        assert rows[1]["metadata"] == {"error_type": "TimeoutError"}

    asyncio.run(scenario())


def test_failed_flush_keeps_the_batch_for_the_next_attempt() -> None:
    class _FlakyStore(MemoryRunEventStore):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        async def put_batch(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("db down")
            return await super().put_batch(events)

    async def scenario() -> None:
        store = _FlakyStore()
        journal = RunJournal(store, _Record())
        await journal.on_chain_start({"name": "lead_agent"}, {}, run_id=uuid4())

        try:
            await journal.flush()
        except RuntimeError as exc:
            assert "db down" in str(exc)
        else:  # pragma: no cover - 失败必须冒泡
            raise AssertionError("put_batch 失败要上抛")
        assert await store.list_events("thread-1", "run-1") == []

        await journal.flush()
        await journal.flush()  # 重试成功后不许再写第二遍
        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RUN_START_EVENT.event_type]

    asyncio.run(scenario())


def test_tokens_are_never_written_and_only_trace_is_truncated() -> None:
    async def scenario() -> None:
        store, journal = _journal(max_trace_content=4)
        run_id = uuid4()

        await journal.on_chain_start({"name": "中文中文中文"}, {}, run_id=run_id)
        await journal.on_llm_new_token("tok", run_id=run_id)
        await journal.on_tool_end(ToolMessage(content="中文中文中文", tool_call_id="c-1"), run_id=run_id)
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RUN_START_EVENT.event_type, LLM_TOOL_RESULT.event_type]
        assert rows[0]["metadata"]["content_truncated"] is True
        assert len(rows[0]["content"].encode("utf-8")) <= 4
        assert rows[1]["content"]["content"] == "中文中文中文", "消息类绝不截断"

    asyncio.run(scenario())


def test_buffer_of_batch_size_is_flushed_without_an_explicit_call() -> None:
    async def scenario() -> None:
        store, journal = _journal(batch_size=2)
        run_id = uuid4()

        await journal.on_chain_start({}, {}, run_id=run_id)
        assert await store.list_events("thread-1", "run-1") == []
        await journal.on_tool_end(ToolMessage(content="42", tool_call_id="c-1"), run_id=run_id)

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RUN_START_EVENT.event_type, LLM_TOOL_RESULT.event_type]

    asyncio.run(scenario())
