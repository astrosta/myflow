"""End-to-end wiring of the explicit-memory tool into the lead graph."""

from __future__ import annotations

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agentkit.agents.memory.openviking.contracts import SessionRef, TurnDecision
from agentkit.agents.memory.openviking.tools import make_memory_remember_tool
from agentkit.agents.middlewares.openviking_memory_middleware import OpenVikingMemoryMiddleware


class ToolFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class RecordingFacade:
    def __init__(self) -> None:
        self.turn_ends: list[TurnDecision] = []
        self.ref = SessionRef("u1", "t1", "u", "s", 0, 1, "i")

    def decide_capture(self, text: str, *, has_attachments: bool = False) -> TurnDecision:
        return TurnDecision("capture")

    async def resolve_capture_context(self, user_id: str, thread_id: str) -> SessionRef:
        return self.ref

    async def publish_human(self, *args, **kwargs) -> None:
        return None

    async def publish_ai(self, *args, **kwargs) -> None:
        return None

    async def publish_tool(self, *args, **kwargs) -> None:
        return None

    async def publish_turn_end(self, session, turn_id, decision, **kwargs) -> None:
        self.turn_ends.append(decision)


@pytest.mark.asyncio
async def test_tool_call_sets_explicit_remember_on_the_middleware_decision() -> None:
    facade = RecordingFacade()
    model = ToolFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "memory_remember",
                        "args": {"text": "我喜欢吃苹果"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="好的"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[make_memory_remember_tool()],
        middleware=[OpenVikingMemoryMiddleware(facade)],
        system_prompt="test",
    )
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}

    await graph.ainvoke(
        {"messages": [HumanMessage(content="请记住我喜欢吃苹果")]},
        context=context,
    )

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0].signals == ("EXPLICIT_REMEMBER",)
    assert context["memory_explicit_remember"] is True
