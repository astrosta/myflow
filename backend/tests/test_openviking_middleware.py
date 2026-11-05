"""Middleware capture path for the additive OpenViking adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import SessionRef, TurnDecision
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
        self.commits: list = []
        self.decision = TurnDecision("capture")
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
        if text == "你好":
            return TurnDecision("skip")
        return self.decision

    async def resolve_capture_context(self, user_id: str, thread_id: str) -> SessionRef:
        return self.ref

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

    async def commit_session(self, session):
        self.commits.append(session)


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
async def test_middleware_does_not_inject_a_profile_message() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    human = HumanMessage(content="我住杭州", id="h1")

    update = await middleware.abefore_agent({"messages": [human]}, runtime(context))

    assert update is None
    assert facade.human == [human]


@pytest.mark.asyncio
async def test_publish_failure_does_not_break_the_turn() -> None:
    facade = FakeFacade()
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
    assert facade.commits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["TASK_COMPLETE", "EXPLICIT_REMEMBER", "USER_CORRECTION"])
async def test_a_commit_signal_is_published_for_the_worker(signal: str) -> None:
    facade = FakeFacade()
    facade.decision = TurnDecision("capture", signals=(signal,))
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="请记住我喜欢吃苹果", id="h1")]},
        runtime(context),
    )

    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][1].signals == (signal,)
    assert facade.commits == []


@pytest.mark.asyncio
async def test_session_end_is_published_without_capturing_the_farewell() -> None:
    facade = FakeFacade()
    facade.decision = TurnDecision("skip", signals=("SESSION_END",))
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}

    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="再见", id="h1")]},
        runtime(context),
    )
    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert facade.human == []
    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][1].signals == ("SESSION_END",)
    assert facade.commits == []


@pytest.mark.asyncio
async def test_detector_adds_a_correction_to_the_turn_end() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="不对，应该是周三", id="h1")]},
        runtime(context),
    )

    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][1].signals == ("USER_CORRECTION",)
    assert facade.commits == []


@pytest.mark.asyncio
async def test_explicit_memory_marker_is_merged_into_turn_end() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="请记住我喜欢吃苹果", id="h1")]},
        runtime(context),
    )
    context["memory_explicit_remember"] = True

    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][1].signals == ("EXPLICIT_REMEMBER",)
    assert facade.commits == []


@pytest.mark.asyncio
async def test_explicit_memory_tool_signal_reaches_turn_end_without_duplication() -> None:
    facade = FakeFacade()
    middleware = OpenVikingMemoryMiddleware(facade)
    context = {"user_id": "u1", "thread_id": "t1", "run_id": "r1"}
    await middleware.abefore_agent(
        {"messages": [HumanMessage(content="请记住我喜欢吃苹果", id="h1")]},
        runtime(context),
    )
    context["capture_decision"] = TurnDecision(
        "capture", signals=("EXPLICIT_REMEMBER",), reason="explicit_tool"
    )

    await middleware.aafter_agent({"messages": []}, runtime(context))

    assert len(facade.turn_ends) == 1
    assert facade.turn_ends[0][1].signals == ("EXPLICIT_REMEMBER",)
    assert facade.commits == []
