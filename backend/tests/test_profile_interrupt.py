"""Graph-level HITL: pause before update_user_profile and resume with Command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agentkit.agents.memory.openviking.profile_interrupt import (
    describe_profile_update,
    make_profile_interrupt_middleware,
)
from agentkit.agents.memory.openviking.profile_tools import make_update_user_profile_tool
from fakes.chat_model import ScriptedChatModel


class FakeUpdater:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def update_fields(self, user_id: str, fields: dict):
        self.calls.append((user_id, dict(fields)))
        return SimpleNamespace(etag="etag-2")


def _graph(updater: FakeUpdater):
    model = ScriptedChatModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_user_profile",
                        "args": {"fields": {"identity.preferred_name": "小林"}},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="已更新"),
        ]
    )
    return create_agent(
        model=model,
        tools=[make_update_user_profile_tool(updater)],
        middleware=[make_profile_interrupt_middleware()],
        checkpointer=InMemorySaver(),
    )


def _config() -> dict:
    return {"configurable": {"thread_id": "profile-hitl-1"}}


@pytest.mark.asyncio
async def test_graph_pauses_before_the_profile_tool_and_approval_writes() -> None:
    updater = FakeUpdater()
    graph = _graph(updater)
    config = _config()
    context = {"user_id": "u1"}

    first = await graph.ainvoke(
        {"messages": [HumanMessage("以后叫我小林")]},
        config=config,
        context=context,
    )

    assert updater.calls == []
    interrupts = first["__interrupt__"]
    assert len(interrupts) == 1
    request = interrupts[0].value
    assert request["action_requests"][0]["name"] == "update_user_profile"
    assert "称呼" in request["action_requests"][0]["description"]

    resumed = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        context=context,
    )

    assert updater.calls == [("u1", {"identity.preferred_name": "小林"})]
    assert any(
        isinstance(message, ToolMessage) and message.status == "success"
        for message in resumed["messages"]
    )


@pytest.mark.asyncio
async def test_reject_resumes_without_writing() -> None:
    updater = FakeUpdater()
    graph = _graph(updater)
    config = _config()
    context = {"user_id": "u1"}

    await graph.ainvoke(
        {"messages": [HumanMessage("以后叫我小林")]},
        config=config,
        context=context,
    )
    resumed = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "reject", "message": "不要改"}]}),
        config=config,
        context=context,
    )

    assert updater.calls == []
    assert any(
        isinstance(message, ToolMessage) and message.status == "error"
        for message in resumed["messages"]
    )


@pytest.mark.asyncio
async def test_history_keeps_the_paused_interrupt_for_refresh() -> None:
    updater = FakeUpdater()
    graph = _graph(updater)
    config = _config()
    await graph.ainvoke(
        {"messages": [HumanMessage("以后叫我小林")]},
        config=config,
        context={"user_id": "u1"},
    )

    from agentkit.runtime.checkpoint_state import build_thread_checkpoint_state_accessor

    accessor = build_thread_checkpoint_state_accessor(
        "profile-hitl-1", graph=graph, checkpointer=graph.checkpointer
    )
    history = [state async for state in accessor.history(None)]

    assert history
    assert history[0]["__interrupt__"][0]["value"]["action_requests"][0]["name"] == (
        "update_user_profile"
    )


def test_description_lists_values_and_note() -> None:
    description = describe_profile_update(
        {
            "name": "update_user_profile",
            "args": {
                "fields": {
                    "identity.preferred_name": "小林",
                    "communication.language": None,
                },
                "note": "用户明确要求",
            },
        },
        {},
        object(),
    )

    assert "称呼" in description
    assert "identity.preferred_name" in description
    assert "删除" in description
    assert "用户明确要求" in description
