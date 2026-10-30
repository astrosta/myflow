"""Middleware capture path for the additive OpenViking adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import (
    PersonalizationDocument,
    PersonalizationView,
    SessionRef,
    TurnDecision,
)
from agentkit.agents.middlewares.openviking_memory_middleware import OpenVikingMemoryMiddleware
from agentkit.runtime.run_finalizers import (
    clear_run_finalizers,
    finish_run_finalizers,
    pending_finalizers,
)


class FakeFacade:
    def __init__(self) -> None:
        self.human: list = []
        self.ai: list = []
        self.tool: list = []
        self.turn_ends: list = []
        self.profile_fields: dict = {}
        self.profile_etag = "etag-1"
        self.profile_error: Exception | None = None
        self.publish_error: Exception | None = None
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

    async def get_personalization(self, user_id):
        if self.profile_error is not None:
            raise self.profile_error
        return PersonalizationView(
            profile_fields=dict(self.profile_fields),
            identity=PersonalizationDocument(content="", source="identity.md", sha256="i"),
            soul=PersonalizationDocument(content="", source="soul.md", sha256="s"),
            profile_etag=self.profile_etag,
            digest="digest",
        )

    async def publish_human(self, session, turn_id, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.human.append(message)

    async def publish_ai(self, session, turn_id, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.ai.append(message)

    async def publish_tool(self, session, turn_id, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.tool.append(message)

    async def publish_turn_end(self, session, turn_id, decision, **kwargs):
        self.turn_ends.append((turn_id, decision, kwargs))


@pytest.fixture(autouse=True)
def _clear_run_finalizers():
    clear_run_finalizers("r1")
    yield
    clear_run_finalizers("r1")


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

    result = await middleware.awrap_tool_call(SimpleNamespace(runtime=runtime(context)), handler)

    assert result is tool
    assert facade.tool == [tool]


@pytest.mark.asyncio
async def test_profile_is_injected_and_replaced_by_etag() -> None:
    facade = FakeFacade()
    facade.profile_fields = {"identity.preferred_name": "小王"}
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    human = HumanMessage(content="我住杭州", id="h1")

    update = await middleware.abefore_agent({"messages": [human]}, runtime(context))
    assert update is not None
    messages = update["messages"]
    assert messages[0].id == "__user_profile__"
    assert messages[-1] is human
    assert messages[0].additional_kwargs["memory_profile"] is True
    assert messages[0].additional_kwargs["memory_profile_etag"] == "etag-1"
    assert "小王" in messages[0].content
    assert "<memory-context" in messages[0].content

    again = await middleware.abefore_agent({"messages": messages}, runtime(context))
    assert again is None

    facade.profile_etag = "etag-2"
    facade.profile_fields = {"identity.preferred_name": "小李"}
    changed = await middleware.abefore_agent({"messages": messages}, runtime(context))
    assert changed is not None
    assert changed["messages"][0].additional_kwargs["memory_profile_etag"] == "etag-2"
    assert len([m for m in changed["messages"] if m.id == "__user_profile__"]) == 1


@pytest.mark.asyncio
async def test_profile_failure_and_publish_failure_do_not_break_the_turn() -> None:
    facade = FakeFacade()
    facade.profile_error = RuntimeError("db down")
    facade.publish_error = RuntimeError("redis down")
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}

    update = await middleware.abefore_agent(
        {"messages": [HumanMessage(content="我住杭州", id="h1")]},
        runtime(context),
    )
    assert update is None
    assert context["capture_decision"].disposition == "capture"


@pytest.mark.asyncio
async def test_cancelled_run_finalizer_publishes_interrupted_turn_end() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="我住杭州", id="h1")]},
        runtime(context),
    )
    assert pending_finalizers("r1") == 1

    await finish_run_finalizers("r1", status="interrupted")

    assert pending_finalizers("r1") == 0
    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][2]["run_status"] == "interrupted"


@pytest.mark.asyncio
async def test_normal_turn_end_and_run_finalizer_do_not_duplicate() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="我住杭州", id="h1")]},
        runtime(context),
    )

    await middleware.aafter_agent({"messages": []}, runtime(context))
    await finish_run_finalizers("r1", status="success")

    assert len(facade.turn_ends) == 1


@pytest.mark.asyncio
async def test_turn_end_is_published_once_after_capture() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="我住杭州", id="h1")]},
        runtime(context),
    )

    await middleware.aafter_agent({"messages": []}, runtime(context))
    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][0] == "r1"
