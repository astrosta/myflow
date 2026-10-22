"""M8 §7.3 / §7.4：checkpoint 血缘，两层回归。

**第 1 层（调用契约）** 防"自己改代码"：子配置里不许出现四个 checkpoint 坐标键。
**第 2 层（生产形状）** 防"升级 LangGraph"：驱动一个**真实的父图**，断言子代理自己的
AI 消息没有被路由进父 run 的 ``messages`` 流——并且用一个负例（显式重新指定坐标）
证明这条断言不是空的。

漏了这一层的后果很具体：用户会看到自己的对话历史突然变成子代理那几条消息。
"""

from __future__ import annotations

import asyncio
import threading
from typing import NamedTuple

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from agentkit.agents.leader_agent.runtime_keys import CHECKPOINT_COORDINATE_KEYS
from agentkit.agents.thread_state import ThreadState
from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.subagents.registry import get_subagent_config
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.tools.builtin.task_tool import SubagentDispatch, make_task_tool
from agentkit.utils.messages import message_content_to_text
from fakes.chat_model import FakeChatModel, ScriptedChatModel
from fakes.task_tool_harness import FakeRuntime, make_app_config, make_tool

SUBAGENT_REPLY = "SUBAGENT-REPLY-XYZ"
LEAD_REPLY = "LEAD-ANSWER"
TOOL_CALL_ID = "call_1"


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    yield
    results.reset_registry()
    ex.shutdown_isolated_subagent_loop()


@pytest.fixture
def subagent_stack(monkeypatch):
    """把子代理侧换成假模型 + 空中间件链：这组测试只关心边界，不关心中间件。"""
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: FakeChatModel(reply=SUBAGENT_REPLY))
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *args, **kwargs: [])


def _dispatch(**overrides) -> SubagentDispatch:
    app_config = overrides.pop("app_config", None) or make_app_config()
    config = overrides.pop("config", None) or get_subagent_config("general-purpose", app_config=app_config)
    defaults = dict(
        prompt="go deep",
        config=config,
        holder=SubagentResult(
            task_id="execution-1",
            trace_id="",
            status=SubagentStatus.PENDING,
            external_task_id=TOOL_CALL_ID,
        ),
        model_name="deepseek-reasoner",
        label="go deep",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        parent_model="deepseek-reasoner",
        uploaded_files=None,
        tools=[],
        allowed_subagents=None,
    )
    defaults.update(overrides)
    return SubagentDispatch(**defaults)


# ---------------------------------------------------------------- §7.3 第 1 层


def test_the_child_config_omits_every_coordinate_key() -> None:
    parent = {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_ns": "tools:abc",
            "checkpoint_id": "1f0",
            "checkpoint_map": {"": "1f0"},
            "__pregel_checkpoint_ns": "tools:abc",
            "__pregel_task_id": "task-1",
            "user_id": "user-1",
        }
    }

    child = ex.build_child_config(parent, max_turns=42)

    assert set(child["configurable"]) & set(CHECKPOINT_COORDINATE_KEYS) == set()
    assert child["recursion_limit"] == 42


def test_the_child_config_keeps_the_hidden_runtime_keys_and_the_rest() -> None:
    """血缘就是靠隐藏键传的：只剔公开坐标，别的一律继承。"""
    parent = {
        "configurable": {
            "thread_id": "thread-1",
            "__pregel_checkpoint_ns": "tools:abc",
            "user_id": "user-1",
        }
    }

    child = ex.build_child_config(parent, max_turns=7)

    assert child["configurable"] == {"__pregel_checkpoint_ns": "tools:abc", "user_id": "user-1"}


def test_a_missing_parent_config_still_yields_a_usable_child_config() -> None:
    assert ex.build_child_config(None, max_turns=9) == {"configurable": {}, "recursion_limit": 9}


# ---------------------------------------------------------------- §7.4 编译参数


def test_the_subagent_graph_has_no_checkpointer_and_no_second_system_prompt(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "agent"

    monkeypatch.setattr(ex, "create_agent", fake_create_agent)
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: "model")
    executor = ex.SubagentExecutor(app_config=make_app_config())

    assert executor._build_agent(_dispatch(), ["mw"]) == "agent"
    assert captured["checkpointer"] is False, "子代理一次性、永不 resume"
    assert captured["system_prompt"] is None, "系统提示在 initial state，插第二条会被严格后端 400"
    assert captured["state_schema"] is ThreadState
    assert captured["middleware"] == ["mw"]
    assert captured["model"] == "model"


def test_the_executor_filters_the_lead_tools_through_the_type(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(ex, "create_agent", lambda **kwargs: captured.update(kwargs) or "agent")
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: "model")
    app_config = make_app_config(allow_host_bash=True, custom_agents={"reader": {"description": "x", "tools": ["read_file"]}})
    executor = ex.SubagentExecutor(app_config=app_config)
    lead_tools = [make_tool("read_file"), make_tool("bash")]

    executor._build_agent(
        _dispatch(app_config=app_config, config=get_subagent_config("reader", app_config=app_config), tools=lead_tools),
        [],
    )

    assert [tool.name for tool in captured["tools"]] == ["read_file"]


def test_the_initial_state_carries_the_role_prompt_and_the_task() -> None:
    dispatch = _dispatch(uploaded_files=[{"filename": "a.txt"}])
    state = ex.SubagentExecutor(app_config=make_app_config())._build_initial_state(dispatch)

    assert isinstance(state["messages"][0], SystemMessage)
    assert "你没有 `task` 工具" in state["messages"][0].content
    assert isinstance(state["messages"][1], HumanMessage)
    assert state["messages"][1].content == "go deep"
    assert state["uploaded_files"] == [{"filename": "a.txt"}]
    assert "promoted" not in state, "§12.4：每次 task 都是一份新鲜 ThreadState"


def test_a_missing_upload_snapshot_is_not_written_into_the_state() -> None:
    dispatch = _dispatch(uploaded_files=None)
    state = ex.SubagentExecutor(app_config=make_app_config())._build_initial_state(dispatch)
    assert "uploaded_files" not in state


def test_the_child_context_carries_identity_and_the_sandbox_lease_id() -> None:
    dispatch = _dispatch()
    context = ex.SubagentExecutor._child_context(dispatch)

    assert context["thread_id"] == "thread-1"
    assert context["run_id"] == "run-1"
    assert context["user_id"] == "user-1"
    assert context["sandbox_lease_owner_id"] == "subagent:execution-1", "§11.1：租约 id 派生自 execution_id"
    assert context["sandbox_command_scope_id"] == context["sandbox_lease_owner_id"]


# ---------------------------------------------------------------- 隔离循环证据


class _LoopProbeModel(FakeChatModel):
    seen: list = []

    def _result(self):
        self.seen.append((threading.current_thread().name, id(asyncio.get_running_loop())))
        return super()._result()


def test_the_subagent_graph_runs_on_the_isolated_loop(subagent_stack, monkeypatch) -> None:
    probe = _LoopProbeModel(reply=SUBAGENT_REPLY)
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: probe)
    app_config = make_app_config()
    tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config), app_config=app_config, poll_interval=0.01
    )

    command = asyncio.run(
        tool.coroutine(
            runtime=FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1"}),
            prompt="go deep",
            subagent_type="general-purpose",
            tool_call_id=TOOL_CALL_ID,
        )
    )

    assert command.update["messages"][0].content.endswith(SUBAGENT_REPLY)
    assert probe.seen, "子代理的模型根本没跑"
    assert probe.seen[0][0] == "subagent-isolated-loop"
    assert probe.seen[0][0] != threading.current_thread().name


# ---------------------------------------------------------------- §7.3 第 2 层


def _parent_graph(task_tool):
    return create_agent(
        model=ScriptedChatModel(
            replies=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "id": TOOL_CALL_ID,
                            "args": {"prompt": "go deep", "subagent_type": "general-purpose"},
                        }
                    ],
                ),
                LEAD_REPLY,
            ]
        ),
        tools=[task_tool],
        middleware=[],
        state_schema=ThreadState,
        checkpointer=InMemorySaver(),
    )


class _Frame(NamedTuple):
    """父 run 的 messages 流里的一帧。"""

    kind: str  # ai / tool / ...
    text: str
    checkpoint_ns: str


async def _stream_parent_messages(graph, thread_id: str) -> list[_Frame]:
    frames: list[_Frame] = []
    async for _mode, chunk in graph.astream(
        {"messages": [HumanMessage("go")]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode=["messages"],
    ):
        message, metadata = chunk
        frames.append(
            _Frame(
                kind=str(getattr(message, "type", "") or ""),
                text=message_content_to_text(getattr(message, "content", "")),
                checkpoint_ns=str(metadata.get("checkpoint_ns") or ""),
            )
        )
    return frames


def _subagent_ai_frames(frames: list[_Frame]) -> list[_Frame]:
    """子代理**自己的** AI 帧：内容就是它的答复原文。

    注意不能用"文本里含没含这段答复"来判断：``task`` 的工具结果本来就把它带回来了
    （那是应该出现的）。要抓的是**多出来的那一帧 AI 消息**。
    """
    return [frame for frame in frames if frame.kind == "ai" and frame.text.strip() == SUBAGENT_REPLY]


def _task_tool(app_config):
    return make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config),
        app_config=app_config,
        poll_interval=0.01,
    )


def test_subagent_frames_never_reach_the_parent_messages_stream(subagent_stack) -> None:
    app_config = make_app_config()
    frames = asyncio.run(_stream_parent_messages(_parent_graph(_task_tool(app_config)), "thread-ok"))
    texts = [frame.text for frame in frames]

    # 父 run 自己的东西照常出现：那个 task 调用、它的工具结果（带子代理的结论）、最终答复
    assert any(text.startswith("任务成功") and SUBAGENT_REPLY in text for text in texts)
    assert LEAD_REPLY in texts
    # 而子代理**自己的** AI 帧没有混进来
    assert _subagent_ai_frames(frames) == []


def test_respecifying_the_coordinates_leaks_subagent_frames(subagent_stack, monkeypatch) -> None:
    """负例：显式重新提供坐标（哪怕值一样）会开启新的根血缘，帧就漏进父 run 了。"""

    def broken(parent_config, *, max_turns):
        configurable = (parent_config or {}).get("configurable") or {}
        return {
            "configurable": {
                "thread_id": configurable.get("thread_id"),
                "checkpoint_ns": configurable.get("checkpoint_ns", ""),
            },
            "recursion_limit": max_turns,
        }

    monkeypatch.setattr(ex, "build_child_config", broken)
    app_config = make_app_config()
    frames = asyncio.run(_stream_parent_messages(_parent_graph(_task_tool(app_config)), "thread-broken"))

    leaked = _subagent_ai_frames(frames)
    assert leaked, "如果这条不再成立，说明 LangGraph 的血缘行为变了——第 2 层的断言要重新检查"
    assert leaked[0].checkpoint_ns, "漏出来的帧会挂在父 run 自己的 namespace 下"
