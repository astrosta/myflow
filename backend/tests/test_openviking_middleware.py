"""Middleware capture path for the additive OpenViking adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import SessionRef, TurnDecision
from agentkit.agents.middlewares.openviking_memory_middleware import OpenVikingMemoryMiddleware


class FakeFacade:
    def __init__(self) -> None:
        self.human: list = []
        self.ai: list = []
        self.tool: list = []
        self.ref = SessionRef(
            user_id="u1",
            thread_id="t1",
            ov_user="u_hash",
            ov_session_id="mf-s-1",
            stream_shard=2,
            session_epoch=1,
            session_incarnation="i1",
        )

    def decide_capture(self, text: str, *, has_attachments: bool = False) -> TurnDecision:
        return TurnDecision("skip" if text == "你好" else "capture")

    async def resolve_capture_context(self, user_id: str, thread_id: str) -> SessionRef:
        return self.ref

    async def publish_human(self, session, turn_id, message):
        self.human.append(message)

    async def publish_ai(self, session, turn_id, message):
        self.ai.append(message)

    async def publish_tool(self, session, turn_id, message):
        self.tool.append(message)


def runtime(context):
    return SimpleNamespace(context=context)


@pytest.mark.asyncio
async def test_skip_does_not_publish() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}

    await middleware.abefore_agent({"messages": [HumanMessage(content="你好")]}, runtime(context))

    assert facade.human == []
    assert context["capture_decision"].disposition == "skip"


@pytest.mark.asyncio
async def test_capture_publishes_human_and_ai_once() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    human = HumanMessage(content="我住杭州", id="h1")
    ai = AIMessage(content="记住了", id="a1")

    await middleware.abefore_agent({"messages": [human]}, runtime(context))
    await middleware.aafter_model({"messages": [human, ai]}, runtime(context))
    await middleware.aafter_model({"messages": [human, ai]}, runtime(context))

    assert facade.human == [human]
    assert facade.ai == [ai]


@pytest.mark.asyncio
async def test_tool_message_is_published_after_handler() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent({"messages": [HumanMessage(content="查一下")]}, runtime(context))
    tool = ToolMessage(content="result", tool_call_id="c1", name="shell")

    async def handler(request):
        return tool

    result = await middleware.awrap_tool_call(SimpleNamespace(), handler)

    assert result is tool
    assert facade.tool == [tool]
