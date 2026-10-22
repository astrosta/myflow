"""M8 §14.3：子代理事件批量落库、只认根命名空间、以及 ``task_id`` 过滤。

``put()`` 是 per-thread 咨询锁的低频路径：``max_turns=150`` 的子代理会在热流循环上打
几百次，所以 worker 侧攒批（阈值 25 / ``subagent.end`` / ``finally`` 三个时机）。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agentkit.config.run_events_config import RunEventsConfig
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.events import DbRunEventStore, JsonlRunEventStore, MemoryRunEventStore
from agentkit.runtime.events.catalog import SUBAGENT_END_EVENT, SUBAGENT_START_EVENT, SUBAGENT_STEP_EVENT
from agentkit.runtime.runs import worker as worker_module
from agentkit.runtime.runs.worker import (
    SUBAGENT_EVENT_FLUSH_THRESHOLD,
    _SubagentEventBuffer,
    _stream_graph,
    is_root_custom_frame,
)
from agentkit.subagents.step_events import SUBAGENT_STEP_MAX_CHARS, subagent_run_event

THREAD_ID = "thread-1"
RUN_ID = "run-1"
TASK_ID = "call_1"


def _record() -> SimpleNamespace:
    # 真的 RunRecord 有 abort_event；流循环每一帧都会查它。
    return SimpleNamespace(
        thread_id=THREAD_ID, run_id=RUN_ID, user_id="user-1", abort_event=threading.Event()
    )


def _step_chunk(index: int = 0, *, task_id: str = TASK_ID) -> dict:
    return {
        "type": "task_running",
        "task_id": task_id,
        "description": "scan the repo",
        "model_name": "deepseek-reasoner",
        "message_index": index,
        "message": {"type": "ai", "id": f"ai-{index}", "content": "step"},
    }


def _end_chunk(*, result: str = "done") -> dict:
    return {
        "type": "task_completed",
        "task_id": TASK_ID,
        "status": "completed",
        "model_name": "deepseek-reasoner",
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "result": result,
    }


class _Store:
    """只记批次：单元测试关心的是"``put_batch`` 被打了几次、装了什么"。"""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    async def put_batch(self, events):
        self.batches.append([dict(event) for event in events])
        return list(events)


# ---------------------------------------------------------------- 帧 → 事件


def test_the_custom_types_map_to_the_three_catalog_events() -> None:
    assert subagent_run_event({"type": "task_started", "task_id": TASK_ID}).event_type == SUBAGENT_START_EVENT.event_type
    assert subagent_run_event(_step_chunk()).event_type == SUBAGENT_STEP_EVENT.event_type
    for custom in ("task_completed", "task_failed", "task_cancelled", "task_timed_out"):
        event = subagent_run_event({"type": custom, "task_id": TASK_ID, "status": "completed"})
        assert event is not None and event.event_type == SUBAGENT_END_EVENT.event_type
        assert event.category == "subagent"


@pytest.mark.parametrize(
    "chunk",
    [
        {"type": "task_running", "message_index": 0, "message": {}},  # 没有 task_id
        {"type": "task_running", "task_id": "  ", "message_index": 0, "message": {}},
        {"type": "task_running", "task_id": TASK_ID, "message": {}},  # 缺 message_index
        {"type": "task_running", "task_id": TASK_ID, "message_index": -1, "message": {}},
        {"type": "task_running", "task_id": TASK_ID, "message_index": 0},  # 缺 message
        {"type": "unknown", "task_id": TASK_ID},  # 不是子代理事件
        "not-a-dict",
    ],
)
def test_malformed_chunks_are_dropped(chunk: object) -> None:
    assert subagent_run_event(chunk) is None


def test_the_end_event_carries_status_model_usage_and_a_bounded_result() -> None:
    event = subagent_run_event(_end_chunk(result="x" * (SUBAGENT_STEP_MAX_CHARS + 100)))

    assert event is not None
    assert event.content["status"] == "completed"
    assert event.content["model_name"] == "deepseek-reasoner"
    assert event.content["usage"]["total_tokens"] == 3
    assert len(event.content["result"]) == SUBAGENT_STEP_MAX_CHARS
    assert event.content["result_truncated"] is True
    assert event.metadata["task_id"] == TASK_ID


def test_step_events_expose_the_message_index_in_metadata() -> None:
    event = subagent_run_event(_step_chunk(7))
    assert event is not None
    assert event.content["message_index"] == 7
    assert event.metadata["message_index"] == 7


# ---------------------------------------------------------------- 批量缓冲


def test_the_threshold_flushes_the_batch() -> None:
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())

    async def scenario() -> None:
        for index in range(SUBAGENT_EVENT_FLUSH_THRESHOLD - 1):
            assert await buffer.record(_step_chunk(index)) is True
        assert store.batches == [], "还没到阈值，一次都没落库"
        assert buffer.pending == SUBAGENT_EVENT_FLUSH_THRESHOLD - 1
        await buffer.record(_step_chunk(99))

    asyncio.run(scenario())
    assert len(store.batches) == 1
    assert len(store.batches[0]) == SUBAGENT_EVENT_FLUSH_THRESHOLD
    assert SUBAGENT_EVENT_FLUSH_THRESHOLD == 25


def test_the_end_event_flushes_immediately() -> None:
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())

    async def scenario() -> None:
        await buffer.record(_step_chunk())
        await buffer.record(_end_chunk())

    asyncio.run(scenario())
    assert [event["event_type"] for event in store.batches[0]] == ["subagent.step", "subagent.end"]
    assert buffer.pending == 0


def test_an_explicit_flush_drains_the_buffer() -> None:
    """worker 的 ``finally`` 走的就是这条：异常 / 取消都要走到。"""
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())

    async def scenario() -> None:
        await buffer.record(_step_chunk())
        assert store.batches == []
        await buffer.flush()
        await buffer.flush()  # 幂等

    asyncio.run(scenario())
    assert len(store.batches) == 1


def test_dropped_chunks_do_not_count_toward_the_threshold() -> None:
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())

    async def scenario() -> bool:
        return await buffer.record({"type": "task_running", "message": {}})

    assert asyncio.run(scenario()) is False
    assert buffer.pending == 0


def test_the_envelope_carries_the_run_identity_and_category() -> None:
    store = _Store()
    buffer = _SubagentEventBuffer(store, _record())

    asyncio.run(buffer.record(_step_chunk()))
    asyncio.run(buffer.flush())

    event = store.batches[0][0]
    assert event["thread_id"] == THREAD_ID and event["run_id"] == RUN_ID
    assert event["user_id"] == "user-1"
    assert event["category"] == "subagent"
    assert event["metadata"]["task_id"] == TASK_ID


# ---------------------------------------------------------------- 命名空间


def test_only_root_custom_frames_are_recorded() -> None:
    """§3.1：带 namespace 的 custom 是子代理自己的事件，再记一次就是自己记自己。"""
    assert is_root_custom_frame("custom", ()) is True
    assert is_root_custom_frame("custom", ("tools:abc",)) is False
    assert is_root_custom_frame("values", ()) is False


class _Graph:
    def __init__(self, items: list) -> None:
        self._items = items

    async def astream(self, *args, **kwargs):
        for item in self._items:
            yield item

    async def aclose(self) -> None:
        return None


class _Bridge:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish(self, run_id, event, payload) -> None:
        self.published.append((event, payload))


class _Usage(AsyncCallbackHandler):
    """``_stream_graph`` 把 usage 挂进 config 的 callbacks，所以它得是真 handler。"""

    async def maybe_report(self) -> None:
        return None


def test_the_stream_loop_records_root_frames_and_ignores_namespaced_ones() -> None:
    store = MemoryRunEventStore()
    buffer = _SubagentEventBuffer(store, _record())
    bridge = _Bridge()
    ctx = SimpleNamespace(
        stream_modes=("custom",),
        run_config={},
        graph_input={"messages": []},
        app_config=None,
    )
    # LangGraph v2 的帧形状：{"type": mode, "ns": (...), "data": payload}
    graph = _Graph(
        [
            {"type": "custom", "ns": (), "data": _step_chunk(0)},  # 根帧：记
            {"type": "custom", "ns": ("tools:abc",), "data": _step_chunk(1)},  # 带 ns：丢
            {"type": "custom", "ns": (), "data": _end_chunk()},  # 终态：立刻 flush
        ]
    )

    asyncio.run(_stream_graph(bridge, _record(), ctx, graph, _Usage(), None, buffer))

    rows = asyncio.run(store.list_events(THREAD_ID, RUN_ID))
    assert [row["event_type"] for row in rows] == ["subagent.step", "subagent.end"]
    assert buffer.pending == 0
    assert len(bridge.published) == 3, "三帧都要照常推给前端，只是两帧不进账本"


def _emitting_graph():
    """**真图**：一个节点里往 custom 通道写子代理事件（形状由 LangGraph 决定，不是我们造的）。"""

    def node(state):
        writer = get_stream_writer()
        writer(_step_chunk(0))
        writer(_end_chunk())
        return {"ok": True}

    graph = StateGraph(dict)
    graph.add_node("n", node)
    graph.add_edge(START, "n")
    graph.add_edge("n", END)
    return graph.compile()


def test_the_worker_subscribes_to_custom_itself() -> None:
    """**持久化不该由客户端的订阅选择决定。**

    ``stream_mode`` 不写时服务端默认是 ``["values"]``，那条流里根本没有 custom 帧——
    而"刷新页面卡片还在"（§17.1）靠的正是这张账本。所以 worker 自己多订一个 custom，
    同时**不**把它推给没订它的客户端。
    """
    store = MemoryRunEventStore()
    buffer = _SubagentEventBuffer(store, _record())
    bridge = _Bridge()
    ctx = SimpleNamespace(
        stream_modes=("values",), run_config={}, graph_input={}, app_config=None
    )

    asyncio.run(_stream_graph(bridge, _record(), ctx, _emitting_graph(), _Usage(), None, buffer))

    rows = asyncio.run(store.list_events(THREAD_ID, RUN_ID))
    assert [row["event_type"] for row in rows] == ["subagent.step", "subagent.end"]
    assert {event for event, _ in bridge.published} == {"values"}, "客户端没订 custom 就不许推"


def test_a_client_that_asked_for_custom_still_gets_it() -> None:
    store = MemoryRunEventStore()
    buffer = _SubagentEventBuffer(store, _record())
    bridge = _Bridge()
    ctx = SimpleNamespace(
        stream_modes=("messages-tuple", "custom"), run_config={}, graph_input={}, app_config=None
    )

    asyncio.run(_stream_graph(bridge, _record(), ctx, _emitting_graph(), _Usage(), None, buffer))

    assert {event for event, _ in bridge.published} == {"custom"}
    assert len(asyncio.run(store.list_events(THREAD_ID, RUN_ID))) == 2

# ---------------------------------------------------------------- task_id 过滤（§14.3）


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


async def _open(backend: str, directory: Path):
    if backend == "memory":
        return MemoryRunEventStore()
    if backend == "jsonl":
        return JsonlRunEventStore(directory / "ledger")
    await init_engine("sqlite", url=_sqlite_url(directory), sqlite_dir=str(directory))
    factory = get_session_factory()
    assert factory is not None
    return DbRunEventStore(factory)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_list_events_filters_by_task_id(backend: str, tmp_path: Path) -> None:
    """展开一张卡片时只拉那一条委派的事件；过滤骑在已有的索引上，不改 schema。"""

    async def scenario() -> None:
        try:
            store = await _open(backend, tmp_path)
            buffer = _SubagentEventBuffer(store, _record())
            await buffer.record(_step_chunk(0, task_id="call_a"))
            await buffer.record(_step_chunk(1, task_id="call_b"))
            await buffer.record(_step_chunk(2, task_id="call_a"))
            await buffer.flush()

            rows = await store.list_events(THREAD_ID, RUN_ID, task_id="call_a")
            assert [row["content"]["message_index"] for row in rows] == [0, 2]

            paged = await store.list_events(
                THREAD_ID, RUN_ID, task_id="call_a", after_seq=rows[0]["seq"]
            )
            assert [row["content"]["message_index"] for row in paged] == [2]

            assert await store.list_events(THREAD_ID, RUN_ID, task_id="nobody") == []
            assert len(await store.list_events(THREAD_ID, RUN_ID)) == 3
        finally:
            await close_engine()

    asyncio.run(scenario())


def test_list_messages_never_returns_subagent_events(tmp_path: Path) -> None:
    """§14.3：用户的主会话只有 lead 的消息和工具结果。"""

    async def scenario() -> None:
        store = MemoryRunEventStore()
        buffer = _SubagentEventBuffer(store, _record())
        await buffer.record(_step_chunk())
        await buffer.record(_end_chunk())
        await buffer.flush()

        assert await store.list_messages(THREAD_ID, limit=50) == []
        assert await store.count_messages(THREAD_ID) == 0
        assert len(await store.list_events(THREAD_ID, RUN_ID)) == 2

    asyncio.run(scenario())
