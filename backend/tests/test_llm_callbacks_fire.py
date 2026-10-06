"""§5.7 的前提：create_agent 走的是 on_chat_model_start，不是 on_llm_start。

这条测试是 langchain 升级改了回调语义时唯一会红的地方 —— 只实现
`on_llm_start` 的 handler 在 create_agent 下一条事件都收不到。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage

from agentkit.runtime.events.catalog import (
    LLM_AI_RESPONSE,
    LLM_HUMAN_INPUT,
    RUN_END_EVENT,
    RUN_START_EVENT,
)
from agentkit.runtime.events.journal import RunJournal
from agentkit.runtime.events.store import MemoryRunEventStore
from fakes.chat_model import FakeChatModel


class _CallbackSpy(AsyncCallbackHandler):
    """账本依赖的那几个回调到底有没有触发。"""

    run_inline = True

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self.seen.append("on_chat_model_start")

    async def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self.seen.append("on_llm_start")

    async def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
        self.seen.append("on_llm_end")

    async def on_llm_error(self, *args: Any, **kwargs: Any) -> None:
        self.seen.append("on_llm_error")


class _Record:
    run_id = "run-1"
    thread_id = "thread-1"
    user_id = "alice"


def test_chat_model_callbacks_fire_once_and_reach_the_ledger() -> None:
    async def scenario() -> None:
        store = MemoryRunEventStore()
        journal = RunJournal(store, _Record())
        spy = _CallbackSpy()
        agent = create_agent(model=FakeChatModel(), tools=[])

        await agent.ainvoke(
            {"messages": [HumanMessage("你好", id="m-1")]},
            config={"callbacks": [journal, spy]},
        )

        assert spy.seen == ["on_chat_model_start", "on_llm_end"], spy.seen
        assert "on_llm_start" not in spy.seen, "chat 模型不触发 on_llm_start"

        rows = await store.list_events("thread-1", "run-1")
        kinds = [row["event_type"] for row in rows]
        assert kinds == [
            RUN_START_EVENT.event_type,
            LLM_HUMAN_INPUT.event_type,
            LLM_AI_RESPONSE.event_type,
            RUN_END_EVENT.event_type,
        ], kinds

        human, answer = rows[1], rows[2]
        assert human["category"] == "message" and human["content"]["content"] == "你好"
        assert human["metadata"]["llm_call_index"] == answer["metadata"]["llm_call_index"] == 0
        assert answer["content"]["content"] == "fake reply"
        assert answer["metadata"]["usage"] == {
            "input_tokens": 3,
            "output_tokens": 5,
            "cache_read_tokens": 0,
        }
        assert answer["metadata"]["caller"] == "lead"
        assert rows[3]["metadata"]["status"] == "success"

    asyncio.run(scenario())


def test_replacing_the_callback_list_loses_the_events() -> None:
    """负例：模型调用自己写 `"callbacks"` 是**替换**继承链，不是追加（§5.7 前提 2）。"""

    async def scenario() -> None:
        store = MemoryRunEventStore()
        journal = RunJournal(store, _Record())  # 挂在父 config 上，被子调用自己的 config 顶掉
        spy = _CallbackSpy()

        await FakeChatModel().ainvoke([HumanMessage("你好")], config={"callbacks": [spy]})

        assert spy.seen == ["on_chat_model_start", "on_llm_end"]
        assert await store.list_events("thread-1", "run-1") == []
        assert journal._buf == []

    asyncio.run(scenario())
