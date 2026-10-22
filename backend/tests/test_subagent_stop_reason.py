"""M8 §13：三条护栏轴的 ``stop_reason``，以及"部分结果算不算完成"。

三条轴（轮次 / token / 循环）**都不新增状态枚举值**：状态仍然是 ``completed`` / ``failed``，
另外带一个可选的 ``stop_reason``。老消费者（前端、台账、跨语言契约）不认识就忽略。

这一节里由执行器负责的部分：轮次轴（``GraphRecursionError`` → ``turn_capped``）与
``consume_stop_reason`` 的 duck typing。token / 循环两条轴的中间件在 D9。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from agentkit.config import AppConfig
from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import FakeChatModel, ScriptedChatModel
from fakes.task_tool_harness import FakeRuntime, make_app_config, make_tool

MODELS = [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    yield
    results.reset_registry()
    ex.shutdown_isolated_subagent_loop()


class FakeGuardrail(AgentMiddleware):
    """``TokenBudget`` / ``LoopDetection`` 的最小形状：执行器只按方法名认它（§12.5）。"""

    def __init__(self, reason: str | None) -> None:
        super().__init__()
        self.reason = reason
        self.seen_run_ids: list[str | None] = []

    def consume_stop_reason(self, run_id):
        self.seen_run_ids.append(run_id)
        return self.reason


def _app_config(*, max_turns: int | None = None) -> AppConfig:
    overrides = {} if max_turns is None else {"general-purpose": {"max_turns": max_turns}}
    return AppConfig.model_validate(
        {
            "models": MODELS,
            "sandbox": {"use": "agentkit.sandbox.local:LocalSandboxProvider"},
            "subagents": {"agents": overrides},
        }
    )


def _run(app_config: AppConfig, *, tools: list | None = None):
    task_tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config),
        app_config=app_config,
        poll_interval=0.005,
    )
    runtime = FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1"})
    runtime.tools = tools or []
    return asyncio.run(
        task_tool.coroutine(
            runtime=runtime,
            prompt="loop forever",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )


def test_hitting_the_turn_cap_is_a_completion_with_a_partial_result(monkeypatch) -> None:
    """§13.1：有可用的最终答复（哪怕只有一部分）→ 仍然是 completed。"""
    looping = AIMessage(
        content="half an answer",
        tool_calls=[{"name": "noop", "id": "call-x", "args": {}}],
    )
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kw: ScriptedChatModel(replies=[looping]))

    command = _run(_app_config(max_turns=2), tools=[make_tool("noop")])
    kwargs = command.update["messages"][0].additional_kwargs

    assert kwargs["subagent_status"] == "completed"
    assert kwargs["subagent_stop_reason"] == "turn_capped"
    assert command.update["delegations"][0]["stop_reason"] == "turn_capped"
    assert "half an answer" in command.update["messages"][0].content


def test_hitting_the_turn_cap_without_any_text_is_a_failure(monkeypatch) -> None:
    """§13.1：什么都没剩下 → failed（**不是** completed）。"""
    looping = AIMessage(content="", tool_calls=[{"name": "noop", "id": "call-x", "args": {}}])
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kw: ScriptedChatModel(replies=[looping]))

    command = _run(_app_config(max_turns=2), tools=[make_tool("noop")])
    kwargs = command.update["messages"][0].additional_kwargs

    assert kwargs["subagent_status"] == "failed"
    assert kwargs["subagent_stop_reason"] == "turn_capped"
    assert "GraphRecursionError" not in command.update["messages"][0].content, "别落进通用 except"


def test_a_guardrail_middleware_reports_its_axis_through_duck_typing(monkeypatch) -> None:
    """§12.5：执行器不 import 那两条轴的中间件，只认 ``consume_stop_reason``。"""
    guardrail = FakeGuardrail("loop_capped")
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [guardrail])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kw: FakeChatModel(reply="done"))

    command = _run(_app_config())
    kwargs = command.update["messages"][0].additional_kwargs

    assert kwargs["subagent_status"] == "completed"
    assert kwargs["subagent_stop_reason"] == "loop_capped"
    assert guardrail.seen_run_ids == ["run-1"], "状态键是 run_id，实例不共享"


def test_a_silent_guardrail_leaves_the_status_clean(monkeypatch) -> None:
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [FakeGuardrail(None)])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kw: FakeChatModel(reply="done"))

    command = _run(_app_config())
    assert "subagent_stop_reason" not in command.update["messages"][0].additional_kwargs
