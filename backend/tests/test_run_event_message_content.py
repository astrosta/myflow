"""事件目录：content 里混进 LangChain 消息对象时，**整批写入不能失败**。

真实故障形态：`journal` 大多数路径已经 `message.model_dump()` 过了，但
`on_chain_*` 的 inputs/outputs、以及中间件塞进来的 payload 里可能带着**没 dump 过**的
`SystemMessage`。jsonl 后端在落盘那一步 `json.dumps(row)` 会 TypeError——
而整批事件是同一个 `put_batch`，于是**一条消息对象让这一批全部丢失**：
日志里只有一条写入异常，读接口那边看起来只是"这几条消息没出现过"。

修法：`codec.json_default` 认 pydantic 对象（`model_dump()`）→ `content` → `str()`，
三档降级、绝不抛；`codec.dumps` 是事件目录里唯一的 JSON 编码器。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agentkit.config.run_events_config import RunEventsConfig
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.runtime.events.catalog import LLM_HUMAN_INPUT, RUN_START_EVENT
from agentkit.persistence.events import codec
from agentkit.persistence.events import (
    DbRunEventStore,
    JsonlRunEventStore,
    MemoryRunEventStore,
    make_run_event_store,
)
from agentkit.persistence.events.codec import (
    dumps,
    encode_for_db,
    json_default,
    limit_trace,
)


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def _event(definition=LLM_HUMAN_INPUT, content: object = "hi", run_id: str = "run-1") -> dict:
    return {
        "thread_id": "thread-1",
        "run_id": run_id,
        "event_type": definition.event_type,
        "category": definition.category,
        "content": content,
        "metadata": {},
    }


# --- codec：三档降级 ---


def test_a_bare_message_becomes_json_that_round_trips() -> None:
    text, metadata = encode_for_db(SystemMessage(content="你是助手"), {}, "message", None)

    assert metadata["content_is_json"] is True
    dumped = json.loads(text)
    assert dumped["type"] == "system"
    assert dumped["content"] == "你是助手"


def test_a_message_nested_in_a_payload_is_dumped_too() -> None:
    """链路的 inputs/outputs 常常是"一层 dict，里面躺着消息"。"""
    text, metadata = encode_for_db(
        {"messages": [SystemMessage(content="sys"), HumanMessage(content="hi")]}, {}, "message", None
    )

    assert metadata["content_is_json"] is True
    assert [item["type"] for item in json.loads(text)["messages"]] == ["system", "human"]


def test_plain_strings_are_stored_verbatim() -> None:
    assert encode_for_db("hello", {}, "message", None) == ("hello", {})


def test_other_scalars_keep_their_type() -> None:
    """以前只有 dict 走 JSON、其余 `str()`：读回来 5 会变成 "5"。"""
    text, metadata = encode_for_db(5, {}, "message", None)

    assert metadata["content_is_json"] is True
    assert json.loads(text) == 5


def test_limit_trace_does_not_choke_on_a_nested_message() -> None:
    content = {"outputs": SystemMessage(content="x" * 50)}

    kept, metadata = limit_trace(content, {}, "trace", 100_000)
    assert kept == content and metadata == {}

    truncated, metadata = limit_trace(content, {}, "trace", 10)
    assert metadata["content_truncated"] is True
    assert isinstance(truncated, str) and len(truncated.encode("utf-8")) <= 10


def test_json_default_degrades_through_all_three_branches() -> None:
    class Duck:
        content = "只有 content"

    assert json_default(SystemMessage(content="hi"))["type"] == "system"
    assert json_default(Duck()) == "只有 content"
    assert json_default(object()) .startswith("<object object at")


def test_a_broken_model_dump_falls_back_instead_of_raising() -> None:
    class Broken:
        content = "还是能救"

        def model_dump(self):  # noqa: ANN201 —— 故意炸
            raise RuntimeError("boom")

    assert json_default(Broken()) == "还是能救"
    assert json.loads(dumps({"value": Broken()}))["value"] == "还是能救"


def test_the_str_fallback_warns_once_per_type(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "每个类型只提示一次"是模块级状态：清掉它，这个用例才不依赖执行顺序
    monkeypatch.setattr(codec, "_warned_non_json_types", set())

    class Weird:
        pass

    with caplog.at_level("WARNING", logger="agentkit.persistence.events.codec"):
        json_default(Weird())
        json_default(Weird())

    warnings = [record for record in caplog.records if "Weird" in record.getMessage()]
    assert len(warnings) == 1, "热路径上按条打日志会把日志刷爆"


# --- jsonl：整批不能因为一条消息对象而失败 ---


def test_jsonl_put_batch_survives_a_system_message(tmp_path: Path) -> None:
    store = JsonlRunEventStore(str(tmp_path))

    async def scenario() -> None:
        rows = await store.put_batch(
            [
                _event(content=SystemMessage(content="系统提示")),
                _event(content={"messages": [HumanMessage(content="hi")]}),
                _event(content="普通字符串"),
            ]
        )

        assert [row["seq"] for row in rows] == [1, 2, 3]
        assert rows[0]["content"]["type"] == "system"
        assert rows[1]["content"]["messages"][0]["content"] == "hi"
        assert rows[2]["content"] == "普通字符串"

        # 真落到盘上、而且下一批的 seq 接着走
        await store.put_batch([_event(content="再来一条", run_id="run-2")])
        assert (await store.list_events("thread-1", "run-2"))[0]["seq"] == 4

        lines = [
            json.loads(line)
            for line in (tmp_path / "threads" / "thread-1" / "runs" / "run-1.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert len(lines) == 3
        assert lines[0]["content"]["type"] == "system"

    _run(scenario)


def test_memory_keeps_the_live_object_while_jsonl_normalizes(tmp_path: Path) -> None:
    """两种后端**故意**不一样，各自都写清楚：

    - 内存后端"放进去什么、拿出来什么"（调试与测试都靠它，run_end 那种整份 outputs
      的载荷也靠它）；
    - 会落盘的 jsonl 必须返回**和盘上一致**的形状——否则同一个 store 的
      `put_batch` 与 `list_events` 对同一条事件给出两种类型。

    真正要防的是第三种情况：把 Python 对象漏到 API/SSE 层，那会变成 500。
    """

    async def scenario() -> None:
        payload = {"messages": [HumanMessage(content="hi")]}
        memory = MemoryRunEventStore()
        rows = await memory.put_batch([_event(content=payload)])
        # 内存后端保留**对象本身**（deepcopy 一份给调用方，免得外面改到账本）
        assert isinstance(rows[0]["content"]["messages"][0], HumanMessage), "内存后端不改内容"

        jsonl = JsonlRunEventStore(str(tmp_path))
        stored = await jsonl.put_batch([_event(content=payload)])
        assert stored[0]["content"]["messages"][0]["type"] == "human"
        assert stored[0]["content"] == (await jsonl.list_events("thread-1", "run-1"))[0]["content"]

    _run(scenario)


def test_jsonl_batch_still_fails_loudly_for_a_real_serialization_bug(tmp_path: Path) -> None:
    """降级不等于把错误吞掉：`default` 里再抛（比如 model_dump 递归爆栈）仍然要冒出来。

    这条守的是"别把 fail-closed 写成 fail-silent"。
    """

    class Hostile:
        content = None

        def model_dump(self):  # noqa: ANN201
            raise RuntimeError("boom")

        def __str__(self) -> str:
            raise RuntimeError("boom too")

    with pytest.raises(RuntimeError):
        dumps({"value": Hostile()})


# --- db：同一条消息也要能回读成结构 ---


def test_db_put_batch_survives_a_system_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = DbRunEventStore(factory)

        rows = await store.put_batch([_event(content=SystemMessage(content="系统提示"))])

        assert rows[0]["content"]["type"] == "system"
        assert rows[0]["metadata"]["content_is_json"] is True
        reloaded = await store.list_events("thread-1", "run-1")
        assert reloaded[0]["content"]["content"] == "系统提示"

    _run(scenario)


def test_make_run_event_store_still_selects_jsonl(tmp_path: Path) -> None:
    store = make_run_event_store(RunEventsConfig(backend="jsonl", jsonl_dir=str(tmp_path)))

    assert isinstance(store, JsonlRunEventStore)
