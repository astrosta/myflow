"""Task 4: subagent retrieval evidence must bypass the 8192-character step cap.

The UI step payload stays bounded, but evaluation needs the full model-visible
retrieval result. These tests pin the second cursor (pure), the holder storage,
the custom-frame mapping in the worker's event buffer, and the parent task
tool's frame emission.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.runtime.events.catalog import (
    RETRIEVAL_KB_EVENT,
    RETRIEVAL_SKILL_EVENT,
    RETRIEVAL_TOOL_EVENT,
)
from agentkit.runtime.runs.worker import _SubagentEventBuffer
from agentkit.subagents.executor import SubagentExecutor, _RunState
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.subagents.step_events import (
    SUBAGENT_STEP_MAX_CHARS,
    build_subagent_step,
    capture_new_retrieval_events,
    subagent_run_event,
)
from agentkit.tools.builtin.task_tool import _report_new_retrievals

THREAD_ID = "thread-1"
RUN_ID = "run-1"
TASK_ID = "call_1"
EXECUTION_ID = "exec-1"

LONG_CONTENT = "升级前先备份数据库。" * 1500


def _knowledge_payload(*, content: str = LONG_CONTENT) -> dict[str, Any]:
    return {
        "query": "升级到 3.2 需要做哪些准备",
        "keyword_query": "3.2 升级 回滚",
        "retrieval_mode": "hybrid",
        "reranked": True,
        "results": [
            {
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "citation_id": "k3f9a2c1",
                "filename": "upgrade.md",
                "section": "准备",
                "page": 3,
                "score": 0.9,
                "rerank_score": 0.44,
                "content": content,
            }
        ],
        "truncated": False,
    }


def _skill_payload() -> dict[str, Any]:
    return {
        "skills": [{"skill_id": "skill-1", "name": "pdf-report"}],
        "retrieval": {
            "channels": ["keyword"],
            "candidates": 1,
            "reranked": False,
            "warnings": [],
        },
        "message": ["只返回元数据；需要正文时请调用 load_skill(name, id)。"],
    }


def _tool(name: str, payload: dict[str, Any] | str, *, id: str, call_id: str = "call-1") -> ToolMessage:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return ToolMessage(content=text, id=id, tool_call_id=call_id, name=name)


def _retrieval_chunk(event_type: str, content: dict[str, Any], *, task_id: str = TASK_ID) -> dict:
    return {
        "type": "task_retrieval",
        "task_id": task_id,
        "retrieval": {"event_type": event_type, "content": content},
    }


def _holder() -> SubagentResult:
    return SubagentResult(
        task_id=EXECUTION_ID,
        trace_id="trace-1",
        status=SubagentStatus.RUNNING,
        external_task_id=TASK_ID,
    )


def _record() -> SimpleNamespace:
    return SimpleNamespace(thread_id=THREAD_ID, run_id=RUN_ID, user_id="user-1")


class _Store:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    async def put_batch(self, events):
        self.batches.append([dict(event) for event in events])
        return list(events)


# ---------------------------------------------------------------- retrieval cursor


def test_a_knowledge_result_longer_than_the_step_cap_keeps_full_content() -> None:
    message = _tool("knowledge_search", _knowledge_payload(), id="tool-1")
    events, cursor = capture_new_retrieval_events([message], 0)

    assert cursor == 1
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == RETRIEVAL_KB_EVENT.event_type == "retrieval.kb"
    item = event["content"]["items"][0]
    assert item["content"] == LONG_CONTENT
    assert len(item["content"]) > SUBAGENT_STEP_MAX_CHARS

    # The UI path is still bounded; only the retrieval cursor reads the full message.
    step = build_subagent_step(message)
    assert len(step["content"]) == SUBAGENT_STEP_MAX_CHARS
    assert step["content_truncated"] is True


def test_search_skills_is_normalized_to_the_skill_event() -> None:
    events, cursor = capture_new_retrieval_events(
        [_tool("search_skills", _skill_payload(), id="tool-skill")], 0
    )

    assert cursor == 1
    assert len(events) == 1
    assert events[0]["event_type"] == RETRIEVAL_SKILL_EVENT.event_type == "retrieval.skill"
    assert events[0]["content"]["skills"] == [{"skill_id": "skill-1", "name": "pdf-report"}]


def test_only_the_new_tail_is_scanned_and_the_cursor_advances() -> None:
    first = _tool("search_skills", _skill_payload(), id="tool-1")
    second = _tool("knowledge_search", _knowledge_payload(), id="tool-2")
    messages = [HumanMessage(content="go"), first]

    events, cursor = capture_new_retrieval_events(messages, 0)
    assert [event["event_type"] for event in events] == ["retrieval.skill"]
    assert cursor == 2

    messages.append(second)
    events, cursor = capture_new_retrieval_events(messages, cursor)
    assert [event["event_type"] for event in events] == ["retrieval.kb"]
    assert cursor == 3

    # Equal length is not a new tail: retrieval evidence must not be emitted twice.
    events, cursor = capture_new_retrieval_events(messages, cursor)
    assert events == []
    assert cursor == 3


def test_a_compaction_shrink_resets_the_cursor_without_replaying_evidence() -> None:
    first = _tool("search_skills", _skill_payload(), id="tool-1")
    messages = [AIMessage(content="", id="ai-1"), first]
    events, cursor = capture_new_retrieval_events(messages, 0)
    assert len(events) == 1 and cursor == 2

    # Summarization rewrites the channel to just the retained tail.
    messages = [first]
    events, cursor = capture_new_retrieval_events(messages, cursor)
    assert events == []
    assert cursor == 1, "游标要跟着新的尾部走，否则之后的证据会永久失明"

    messages.append(_tool("knowledge_search", _knowledge_payload(), id="tool-2"))
    events, cursor = capture_new_retrieval_events(messages, cursor)
    assert [event["event_type"] for event in events] == ["retrieval.kb"]
    assert cursor == 2


def test_non_retrieval_tools_and_unavailable_payloads_emit_nothing() -> None:
    messages = [
        _tool("read_file", "plain text", id="tool-1"),
        _tool(
            "search_skills",
            {"skills": [], "message": ["技能检索暂不可用（技能目录未就绪）。"]},
            id="tool-2",
        ),
        _tool("knowledge_search", '{"error": {"code": "knowledge_disabled"}}', id="tool-3"),
        _tool("knowledge_search", '{"results": [', id="tool-4"),
    ]

    events, cursor = capture_new_retrieval_events(messages, 0)

    assert events == []
    assert cursor == len(messages)


# ---------------------------------------------------------------- holder storage


def test_the_holder_appends_and_reports_retrievals_with_a_cursor() -> None:
    holder = _holder()
    first = {"event_type": "retrieval.kb", "content": {"query": "q1"}}
    second = {"event_type": "retrieval.skill", "content": {"skills": []}}

    holder.append_retrievals([first])
    assert holder.pending_retrievals(0) == [first]
    assert holder.pending_retrievals(1) == []

    holder.append_retrievals([second])
    assert holder.pending_retrievals(1) == [second]
    holder.append_retrievals([])
    assert holder.pending_retrievals(1) == [second]


def test_the_executor_capture_appends_retrievals_and_keeps_steps_bounded() -> None:
    holder = _holder()
    run = _RunState()
    message = _tool("knowledge_search", _knowledge_payload(), id="tool-1")

    SubagentExecutor()._capture_steps(run, holder, {"messages": [message]})

    pending = holder.pending_retrievals(0)
    assert len(pending) == 1
    assert pending[0]["content"]["items"][0]["content"] == LONG_CONTENT
    steps = holder.pending_steps(0)
    assert len(steps) == 1
    assert len(steps[0]["message"]["content"]) == SUBAGENT_STEP_MAX_CHARS
    assert run.retrieval_processed_count == 1


# ---------------------------------------------------------------- frame mapping


def _kb_event_content() -> dict[str, Any]:
    """归一化之后的 KB 载荷：带 query 和 items，不是工具原始报文。"""

    return {
        "query": "升级到 3.2 需要做哪些准备",
        "items": [{"document_id": "doc-1", "chunk_id": "chunk-1", "content": LONG_CONTENT}],
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("event_type", "content", "expected"),
    [
        ("retrieval.kb", _kb_event_content(), RETRIEVAL_KB_EVENT),
        (
            "retrieval.skill",
            {
                "skills": [{"skill_id": "skill-1", "name": "pdf-report"}],
                "channels": ["keyword"],
                "warnings": [],
                "truncated": False,
            },
            RETRIEVAL_SKILL_EVENT,
        ),
        (
            "retrieval.tool",
            {"tools": [{"call_name": "read_file", "tool_id": "t-1", "score": 0.5}], "channels": []},
            RETRIEVAL_TOOL_EVENT,
        ),
    ],
)
def test_task_retrieval_maps_to_a_retrieval_row_with_task_id(
    event_type: str, content: dict[str, Any], expected
) -> None:
    event = subagent_run_event(_retrieval_chunk(event_type, content))

    assert event is not None
    assert event.event_type == expected.event_type
    assert event.category == "retrieval"
    assert event.metadata == {"task_id": TASK_ID}
    assert event.content == content


@pytest.mark.parametrize(
    "chunk",
    [
        {"type": "task_retrieval", "retrieval": {"event_type": "retrieval.kb", "content": {}}},
        {"type": "task_retrieval", "task_id": " ", "retrieval": {}},
        {"type": "task_retrieval", "task_id": TASK_ID},
        {"type": "task_retrieval", "task_id": TASK_ID, "retrieval": "nope"},
        {
            "type": "task_retrieval",
            "task_id": TASK_ID,
            "retrieval": {"event_type": "retrieval.tool", "content": {}},
        },
        {
            "type": "task_retrieval",
            "task_id": TASK_ID,
            "retrieval": {"event_type": "retrieval.kb", "content": "nope"},
        },
    ],
)
def test_malformed_task_retrieval_frames_are_dropped(chunk: object) -> None:
    assert subagent_run_event(chunk) is None


@pytest.mark.parametrize(
    "event_type",
    [
        ["retrieval.kb"],
        {"event_type": "retrieval.skill"},
        ("retrieval.kb",),
    ],
)
def test_a_non_string_event_type_is_dropped_not_raised(event_type: object) -> None:
    """非字符串 event_type 不能当 dict key：list/dict 会抛 TypeError，坏帧必须只丢不炸。"""

    chunk = {
        "type": "task_retrieval",
        "task_id": TASK_ID,
        "retrieval": {"event_type": event_type, "content": _kb_event_content()},
    }

    assert subagent_run_event(chunk) is None


@pytest.mark.parametrize(
    ("event_type", "content"),
    [
        # retrieval.kb：必须有非空 query 和 items 列表
        ("retrieval.kb", {}),
        ("retrieval.kb", {"query": "升级到 3.2 需要做哪些准备"}),
        ("retrieval.kb", {"items": []}),
        ("retrieval.kb", {"query": " ", "items": []}),
        ("retrieval.kb", {"query": "升级", "items": "nope"}),
        ("retrieval.kb", {"query": 42, "items": []}),
        # 工具原始报文（results）不是归一化载荷：形状不对就丢，别把原文当行落库
        ("retrieval.kb", _knowledge_payload()),
        # retrieval.skill：必须有 skills 列表
        ("retrieval.skill", {}),
        ("retrieval.skill", {"channels": []}),
        ("retrieval.skill", {"skills": {"skill_id": "skill-1"}}),
        # retrieval.tool：必须有 tools 列表
        ("retrieval.tool", {}),
        ("retrieval.tool", {"tools": {}}),
        ("retrieval.tool", {"channels": ["keyword"]}),
    ],
)
def test_a_valid_task_id_with_unusable_content_is_dropped(
    event_type: str, content: dict[str, Any]
) -> None:
    """空对象 / 缺载荷键不是"检索到零条"，是坏帧：不构造行，返回 None。"""

    assert subagent_run_event(_retrieval_chunk(event_type, content)) is None


def test_an_empty_payload_list_is_still_a_valid_row() -> None:
    """"检索到了零条"和"没有载荷"不是一回事：空列表配上 query 仍是一行证据。"""

    content = {"query": "升级到 3.2 需要做哪些准备", "items": [], "truncated": False}

    event = subagent_run_event(_retrieval_chunk("retrieval.kb", content))

    assert event is not None
    assert event.event_type == RETRIEVAL_KB_EVENT.event_type
    assert event.content["items"] == []


def test_the_event_buffer_persists_full_kb_content_with_task_id() -> None:
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())
    content = {
        "query": "升级到 3.2 需要做哪些准备",
        "items": [{"document_id": "doc-1", "chunk_id": "chunk-1", "content": LONG_CONTENT}],
        "truncated": False,
    }

    async def scenario() -> bool:
        recorded = await buffer.record(
            _retrieval_chunk(RETRIEVAL_KB_EVENT.event_type, content)
        )
        await buffer.flush()
        return recorded

    assert asyncio.run(scenario()) is True
    row = store.batches[0][0]
    assert row["event_type"] == "retrieval.kb"
    assert row["category"] == "retrieval"
    assert row["metadata"] == {"task_id": TASK_ID}
    assert row["content"]["items"][0]["content"] == LONG_CONTENT
    assert len(row["content"]["items"][0]["content"]) > SUBAGENT_STEP_MAX_CHARS


# ---------------------------------------------------------------- parent task tool


def test_the_parent_task_tool_reports_pending_retrieval_frames() -> None:
    emitted: list[dict[str, Any]] = []
    runtime = SimpleNamespace(stream_writer=emitted.append)
    holder = _holder()
    event = {
        "event_type": "retrieval.kb",
        "content": {"query": "q", "items": [{"content": LONG_CONTENT}], "truncated": False},
    }
    holder.append_retrievals([event])

    async def scenario() -> int:
        reported = await _report_new_retrievals(holder, runtime, reported=0)
        assert await _report_new_retrievals(holder, runtime, reported=reported) == reported
        return reported

    assert asyncio.run(scenario()) == 1
    assert len(emitted) == 1
    frame = emitted[0]
    assert frame["type"] == "task_retrieval"
    assert frame["task_id"] == TASK_ID
    assert frame["retrieval"] == event
