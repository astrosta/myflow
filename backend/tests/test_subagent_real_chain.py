"""跑**真实**子代理中间件链的用例：轮次预算与运行时上下文。

``max_turns`` 是模型调用次数，执行器按编译后的图换算 ``recursion_limit``。LangGraph 的
``recursion_limit`` 数的是图的超步，每个中间件钩子节点都算一步；把 ``max_turns`` 原样当
``recursion_limit``，检索子代理连一次工具调用都做不完。这类问题只在真实链下出现，
所以这里不要把 ``build_subagent_runtime_middlewares`` 换成 ``[]``。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from pydantic import Field

from agentkit.agents.thread_state import ThreadState
from agentkit.config import AppConfig
from agentkit.config import paths as paths_module
from agentkit.config.paths import Paths
from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import ScriptedChatModel
from fakes.task_tool_harness import FakeRuntime, make_tool

#: 每个内置类型的提示词允许的最多工具轮数（检索类按 system_prompt 的次数上限）。
ROUNDS_ALLOWED_BY_PROMPT = {
    "knowledge-researcher": ("knowledge_search", 3),
    "memory-researcher": ("memory_search", 5),
    "skill-finder": ("search_skills", 1),
    "general-purpose": ("read_file", 10),
    "bash": ("bash", 10),
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(paths_module, "_paths", Paths(tmp_path))
    results.reset_registry()
    yield
    results.reset_registry()
    ex.shutdown_isolated_subagent_loop()


def _app_config(**subagents) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {
                "use": "agentkit.sandbox.local:LocalSandboxProvider",
                "allow_host_bash": True,
            },
            "knowledge_base": {
                "enabled": True,
                "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
                "embedding": {"base_url": "http://embedding:8000/v1"},
                "vector_store": {"uri": "http://milvus:19530"},
            },
            "memory": {"openviking": {"enabled": True, "user_id_salt": "s" * 32}},
            "subagents": subagents,
        }
    )


def _tool_rounds(tool_name: str, rounds: int) -> list[AIMessage]:
    return [
        AIMessage(content="", tool_calls=[{"name": tool_name, "id": f"call-{index}", "args": {}}])
        for index in range(rounds)
    ]


class RecordingChatModel(ScriptedChatModel):
    """按脚本回复，并记下每次调用时模型收到的消息。"""

    seen: list = Field(default_factory=list)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def _delegate(monkeypatch, app_config: AppConfig, *, subagent_type: str, tool_name: str, replies):
    model = RecordingChatModel(replies=replies)
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: model)
    tool = make_tool(tool_name)
    task_tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config),
        app_config=app_config,
        poll_interval=0.005,
        delegated_tools=[tool],
    )
    runtime = FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1"})
    runtime.tools = [tool]
    command = asyncio.run(
        task_tool.coroutine(
            runtime=runtime,
            prompt="do it",
            subagent_type=subagent_type,
            tool_call_id="call_1",
        )
    )
    return command.update["messages"][0], model


@pytest.mark.parametrize("subagent_type", sorted(ROUNDS_ALLOWED_BY_PROMPT))
def test_every_builtin_finishes_the_rounds_its_prompt_allows(monkeypatch, subagent_type) -> None:
    tool_name, rounds = ROUNDS_ALLOWED_BY_PROMPT[subagent_type]
    replies = [*_tool_rounds(tool_name, rounds), AIMessage(content="FINAL ANSWER")]

    message, model = _delegate(
        monkeypatch,
        _app_config(),
        subagent_type=subagent_type,
        tool_name=tool_name,
        replies=replies,
    )

    assert message.additional_kwargs["subagent_status"] == "completed", message.content
    assert "subagent_stop_reason" not in message.additional_kwargs
    assert "FINAL ANSWER" in message.content
    assert model.calls == rounds + 1


def test_max_turns_is_the_number_of_model_calls(monkeypatch) -> None:
    looping = AIMessage(content="half", tool_calls=[{"name": "read_file", "id": "call-x", "args": {}}])

    message, model = _delegate(
        monkeypatch,
        _app_config(agents={"general-purpose": {"max_turns": 3}}),
        subagent_type="general-purpose",
        tool_name="read_file",
        replies=[looping],
    )

    assert model.calls == 3
    assert message.additional_kwargs["subagent_stop_reason"] == "turn_capped"


def test_the_subagent_sees_todays_date(monkeypatch) -> None:
    """"上周""最近一个月"要换算成 since/until，子代理得知道今天是哪天。"""
    message, model = _delegate(
        monkeypatch,
        _app_config(),
        subagent_type="memory-researcher",
        tool_name="memory_search",
        replies=[AIMessage(content="FINAL ANSWER")],
    )

    assert message.additional_kwargs["subagent_status"] == "completed"
    first_call = model.seen[0]
    assert any("<current_date>" in str(item.content) for item in first_call)


def test_a_graph_without_middleware_or_tools_still_gets_room_for_its_turns() -> None:
    agent = create_agent(
        model=ScriptedChatModel(replies=["done"]),
        tools=[],
        middleware=[],
        system_prompt=None,
        state_schema=ThreadState,
        checkpointer=False,
    )

    assert ex.recursion_limit_for(agent, 1) == 2
    assert ex.recursion_limit_for(agent, 5) == 10
