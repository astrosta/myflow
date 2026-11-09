"""Task 3：RunJournal 为 lead 侧的 tool_search 结果写 retrieval.tool 证据。

检索事件是评测样本的原始证据（定义与归一化器在 Task 2）。这里只测账本端的两件事：
什么样的消息该产出一行，以及"回调重放 / 对账补写"这条重复路径不能让它变成两行。
归一化器自己说"这条结果不算检索证据"时，账本必须什么都不写。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage

from agentkit.persistence.events import MemoryRunEventStore
from agentkit.runtime.events.catalog import LLM_TOOL_RESULT, RETRIEVAL_TOOL_EVENT
from agentkit.runtime.events.journal import RunJournal

TOOL_SEARCH = "tool_search"


class _Record:
    run_id = "run-1"
    thread_id = "thread-1"
    user_id = "alice"


def _journal() -> tuple[MemoryRunEventStore, RunJournal]:
    store = MemoryRunEventStore()
    return store, RunJournal(store, _Record())


def _kinds(rows: list[dict[str, Any]]) -> list[str]:
    return [row["event_type"] for row in rows]


def _card(call_name: str, score: float) -> dict[str, Any]:
    """tool_search 的一张工具卡：字段与 agentkit.tools.builtin.tool_search 一致。"""
    return {
        "tool_id": f"id-{call_name}",
        "tool_name": call_name.rsplit("__", 1)[-1],
        "server_name": "files",
        "call_name": call_name,
        "description": "读文件",
        "tags": ["filesystem"],
        "parameters": ["path"],
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "schema_truncated": False,
        "score": score,
    }


def _tool_search_result(
    *call_names: str, index: str = "milvus", truncated: bool = False
) -> str:
    """一次成功检索的模型可见结果（tool_search 用 json.dumps 交出）。"""
    return json.dumps(
        {
            "tools": [_card(name, 0.9 - offset / 10) for offset, name in enumerate(call_names)],
            "truncated": truncated,
            "retrieval": {
                "index": index,
                "channels": ["vector"],
                "candidates": len(call_names),
                "reranked": True,
                "warnings": [],
            },
            "message": "Call tool_execute(tool_name, arguments) to run one of these tools.",
        },
        ensure_ascii=False,
    )


def _unavailable_result() -> str:
    """检索没有发生：这不是"检索到 0 条"，账本不能把它记成一条证据。"""
    return json.dumps(
        {
            "tools": [],
            "retrieval": {
                "index": "unavailable",
                "channels": [],
                "candidates": 0,
                "reranked": False,
                "warnings": ["catalog_unavailable"],
            },
            "message": "Tool retrieval is unavailable in this deployment.",
        },
        ensure_ascii=False,
    )


def _tool_message(content: str, *, tool_call_id: str = "c1", name: str = TOOL_SEARCH) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name, status="success")


def test_tool_end_writes_a_retrieval_row_after_the_tool_result() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        message = _tool_message(_tool_search_result("mcp__files__read_file", "mcp__files__list_dir"))

        await journal.on_tool_end(message, run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        # 顺序是契约：先有模型可见的工具结果，才有从它派生的证据
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, RETRIEVAL_TOOL_EVENT.event_type]
        retrieval = rows[1]
        assert retrieval["category"] == "retrieval"
        assert retrieval["metadata"] == {"tool_name": TOOL_SEARCH}
        assert [card["call_name"] for card in retrieval["content"]["tools"]] == [
            "mcp__files__read_file",
            "mcp__files__list_dir",
        ]
        assert retrieval["content"]["channels"] == ["vector"]
        assert retrieval["content"]["truncated"] is False

    asyncio.run(scenario())


def test_a_replayed_tool_callback_does_not_duplicate_the_retrieval_row() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        message = _tool_message(_tool_search_result("mcp__files__read_file"))

        await journal.on_tool_end(message, run_id=uuid4())
        # 重放：同一次工具调用被再次交出（新的 ToolMessage 实例，同一个 tool_call_id）
        await journal.on_tool_end(_tool_message(message.content), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, RETRIEVAL_TOOL_EVENT.event_type]

    asyncio.run(scenario())


def test_reconcile_writes_retrieval_evidence_for_a_message_the_callback_missed() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        outputs = {"messages": [_tool_message(_tool_search_result("mcp__files__read_file"))]}

        await journal.on_chain_end(outputs, run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [
            LLM_TOOL_RESULT.event_type,
            RETRIEVAL_TOOL_EVENT.event_type,
            "run.end",
        ]
        assert rows[1]["metadata"] == {"tool_name": TOOL_SEARCH}
        assert rows[1]["content"]["tools"][0]["call_name"] == "mcp__files__read_file"

    asyncio.run(scenario())


def test_reconcile_after_tool_end_does_not_duplicate_the_retrieval_row() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        message = _tool_message(_tool_search_result("mcp__files__read_file"))

        await journal.on_tool_end(message, run_id=uuid4())
        await journal.on_chain_end({"messages": [message]}, run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [
            LLM_TOOL_RESULT.event_type,
            RETRIEVAL_TOOL_EVENT.event_type,
            "run.end",
        ]

    asyncio.run(scenario())


def test_unavailable_retrieval_writes_no_retrieval_row() -> None:
    async def scenario() -> None:
        store, journal = _journal()

        await journal.on_tool_end(_tool_message(_unavailable_result()), run_id=uuid4())
        await journal.on_chain_end({"messages": []}, run_id=uuid4())

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, "run.end"]

    asyncio.run(scenario())


def test_other_tools_never_write_retrieval_rows() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        # 非检索工具：内容和形状都无所谓，一条检索证据都不该冒出来
        await journal.on_tool_end(
            _tool_message(_tool_search_result("mcp__files__read_file"), name="calc"),
            run_id=uuid4(),
        )
        await journal.on_tool_end("不是 ToolMessage：不写", run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type]

    asyncio.run(scenario())


def test_a_successful_search_that_found_nothing_is_still_recorded() -> None:
    async def scenario() -> None:
        store, journal = _journal()

        await journal.on_tool_end(_tool_message(_tool_search_result()), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, RETRIEVAL_TOOL_EVENT.event_type]
        # 检索发生了但没有命中——这是证据，不能和"检索不可用"混为一谈
        assert rows[1]["content"]["tools"] == []

    asyncio.run(scenario())


def test_retrieval_name_falls_back_to_the_tool_call_id_mapping() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        await journal.on_tool_start(
            {"name": TOOL_SEARCH},
            "",
            run_id=uuid4(),
            metadata={"tool_call_id": "c9"},
        )
        message = ToolMessage(
            content=_tool_search_result("mcp__files__read_file"),
            tool_call_id="c9",
            status="success",
        )

        await journal.on_tool_end(message, run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, RETRIEVAL_TOOL_EVENT.event_type]
        assert rows[0]["metadata"]["tool_name"] == TOOL_SEARCH
        assert rows[1]["metadata"] == {"tool_name": TOOL_SEARCH}

    asyncio.run(scenario())


def test_cross_path_replay_of_a_fresh_message_does_not_duplicate_retrieval() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        message = _tool_message(_tool_search_result("mcp__files__read_file"))

        await journal.on_tool_end(message, run_id=uuid4())
        # 对账交出的是一份新实例：消息身份拦不住它，只有 retrieval 身份能拦
        await journal.on_chain_end(
            {"messages": [_tool_message(message.content)]}, run_id=uuid4()
        )

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [
            LLM_TOOL_RESULT.event_type,
            RETRIEVAL_TOOL_EVENT.event_type,
            "run.end",
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("tool_name", ["search_skills", "knowledge_search"])
def test_skill_and_knowledge_tool_messages_write_no_retrieval_tool_row(
    tool_name: str,
) -> None:
    async def scenario() -> None:
        store, journal = _journal()

        await journal.on_tool_end(
            _tool_message(_tool_search_result("mcp__files__read_file"), name=tool_name),
            run_id=uuid4(),
        )
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type]

    asyncio.run(scenario())


def test_malformed_tool_search_json_keeps_the_tool_result_but_writes_no_evidence() -> None:
    async def scenario() -> None:
        store, journal = _journal()

        await journal.on_tool_end(_tool_message("{not json"), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type]

    asyncio.run(scenario())


def test_a_none_payload_does_not_poison_the_retrieval_identity() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        tool_call_id = "c-none-first"

        await journal._emit_retrieval(_tool_message("not json", tool_call_id=tool_call_id))
        await journal._emit_retrieval(
            _tool_message(
                _tool_search_result("mcp__files__read_file"), tool_call_id=tool_call_id
            )
        )
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [RETRIEVAL_TOOL_EVENT.event_type]

    asyncio.run(scenario())


def test_a_budget_truncated_result_is_recorded_as_truncated() -> None:
    async def scenario() -> None:
        store, journal = _journal()

        await journal.on_tool_end(
            _tool_message(_tool_search_result("mcp__files__read_file", truncated=True)),
            run_id=uuid4(),
        )
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        assert _kinds(rows) == [LLM_TOOL_RESULT.event_type, RETRIEVAL_TOOL_EVENT.event_type]
        assert rows[1]["content"]["truncated"] is True

    asyncio.run(scenario())


def test_retrieval_dedups_by_content_when_the_message_has_no_call_id() -> None:
    async def scenario() -> None:
        store, journal = _journal()
        content = _tool_search_result("mcp__files__read_file")

        await journal.on_tool_end(_tool_message(content, tool_call_id=""), run_id=uuid4())
        await journal.on_tool_end(_tool_message(content, tool_call_id=""), run_id=uuid4())
        await journal.flush()

        rows = await store.list_events("thread-1", "run-1")
        retrieval_rows = [
            row for row in rows if row["event_type"] == RETRIEVAL_TOOL_EVENT.event_type
        ]
        assert len(retrieval_rows) == 1

    asyncio.run(scenario())
