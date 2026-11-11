"""子代理 token 预算：解析、硬停与执行器接线。

这里钉四件事：

1. 全局预算与类型级 token_budget 覆盖按文档顺序解析；
2. 到上限时剥掉 tool_calls、强制 stop，并保留已经产出的文本；
3. 执行器在真实子代理链上会把累计用量读到，而不是只读当前上下文；
4. 关闭 token_budget 时不注入中间件，不影响原来的轮次轴。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from langchain_core.messages import AIMessage

from agentkit.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_EMPTY_TAIL,
    TOKEN_BUDGET_NO_RESULT_KEY,
    TokenBudgetMiddleware,
)
from agentkit.config import AppConfig
from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import ScriptedChatModel
from fakes.task_tool_harness import FakeRuntime, make_tool

MODELS = [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    yield
    results.reset_registry()
    ex.shutdown_isolated_subagent_loop()


def _holder(total_tokens: int) -> SubagentResult:
    holder = SubagentResult(
        task_id="exec-1",
        trace_id="trace-1",
        status=SubagentStatus.PENDING,
    )
    holder.update_token_usage_records([{"input_tokens": total_tokens, "output_tokens": 0, "total_tokens": total_tokens}])
    return holder


def _middleware(
    holder: SubagentResult,
    *,
    max_tokens: int = 100,
    warn_threshold: float = 0.7,
) -> TokenBudgetMiddleware:
    return TokenBudgetMiddleware(
        max_tokens=max_tokens,
        warn_threshold=warn_threshold,
        usage_reader=holder.total_tokens,
        subagent_name="general-purpose",
    )


def _message(content: str = "half an answer") -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[{"name": "noop", "id": "call-1", "args": {}}],
    )


def _app_config(**subagents) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": MODELS,
            "sandbox": {"use": "agentkit.sandbox.local:LocalSandboxProvider"},
            "subagents": subagents,
        }
    )


# ---------------------------------------------------------------- 中间件本身


def test_under_budget_does_not_touch_the_message() -> None:
    middleware = _middleware(_holder(50))
    message = _message()

    assert middleware.after_model({"messages": [message]}, None) is None
    assert middleware.consume_stop_reason("run-1") is None


def test_over_budget_strips_tool_calls_and_keeps_partial_text() -> None:
    middleware = _middleware(_holder(101), max_tokens=100)
    message = _message("half an answer")

    update = middleware.after_model({"messages": [message]}, None)

    assert update is not None
    stripped = update["messages"][0]
    assert stripped.tool_calls == []
    assert stripped.additional_kwargs.get("tool_calls") in (None, [])
    assert "half an answer" in stripped.content
    assert "token 预算" in stripped.content
    assert stripped.response_metadata["finish_reason"] == "stop"
    assert middleware.consume_stop_reason("run-1") == "token_capped"


def test_over_budget_without_text_says_no_partial_answer() -> None:
    middleware = _middleware(_holder(101), max_tokens=100)

    update = middleware.after_model({"messages": [_message("")]}, None)

    assert update is not None
    stripped = update["messages"][0]
    assert stripped.tool_calls == []
    assert TOKEN_BUDGET_EMPTY_TAIL in stripped.content
    assert stripped.additional_kwargs[TOKEN_BUDGET_NO_RESULT_KEY] is True


def test_extract_final_result_does_not_reuse_an_earlier_thought_after_an_empty_cap() -> None:
    earlier = AIMessage(content="I will search the web first.")
    capped = AIMessage(
        content=TOKEN_BUDGET_EMPTY_TAIL,
        additional_kwargs={TOKEN_BUDGET_NO_RESULT_KEY: True},
    )

    assert ex.extract_final_result({"messages": [earlier, capped]}) is None


def test_warning_threshold_is_logged_once(caplog) -> None:
    middleware = _middleware(_holder(75), max_tokens=100, warn_threshold=0.7)

    with caplog.at_level(logging.WARNING):
        middleware.after_model({"messages": [_message()]}, None)
        middleware.after_model({"messages": [_message()]}, None)

    assert caplog.text.count("warning threshold") == 1


def test_a_broken_usage_reader_does_not_break_the_agent() -> None:
    def broken() -> int:
        raise RuntimeError("counter down")

    middleware = TokenBudgetMiddleware(
        max_tokens=1,
        warn_threshold=0.7,
        usage_reader=broken,
        subagent_name="general-purpose",
    )

    assert middleware.after_model({"messages": [_message()]}, None) is None
    assert middleware.consume_stop_reason("run-1") is None


# ---------------------------------------------------------------- 执行器接线


def _run(monkeypatch, *, budget: int):
    app_config = _app_config(
        agents={"general-purpose": {"token_budget": {"max_tokens": budget}}}
    )
    looping = AIMessage(
        content="half an answer",
        tool_calls=[{"name": "noop", "id": "call-x", "args": {}}],
    )
    model = ScriptedChatModel(replies=[looping])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: model)
    tool = make_tool("noop")
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
            prompt="research",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )
    return command, model


def test_executor_stops_a_looping_subagent_at_the_token_budget(monkeypatch) -> None:
    command, model = _run(monkeypatch, budget=20)
    message = command.update["messages"][0]
    kwargs = message.additional_kwargs

    assert model.calls == 3, "每次 8 token，第三次后累计 24，应该在这里停"
    assert kwargs["subagent_status"] == "completed"
    assert kwargs["subagent_stop_reason"] == "token_capped"
    assert "token 预算" in message.content
    assert command.update["delegations"][0]["stop_reason"] == "token_capped"


def test_executor_reports_a_capped_subagent_without_text_as_failed(monkeypatch) -> None:
    app_config = _app_config(
        agents={"general-purpose": {"token_budget": {"max_tokens": 20}}}
    )
    looping = AIMessage(
        content="",
        tool_calls=[{"name": "noop", "id": "call-x", "args": {}}],
    )
    model = ScriptedChatModel(replies=[looping])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: model)
    tool = make_tool("noop")
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
            prompt="research",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )

    kwargs = command.update["messages"][0].additional_kwargs
    assert kwargs["subagent_status"] == "failed"
    assert kwargs["subagent_stop_reason"] == "token_capped"
    assert "结果" not in command.update["messages"][0].content


def test_executor_does_not_inject_a_budget_when_the_type_disables_it(monkeypatch) -> None:
    app_config = _app_config(
        agents={
            "general-purpose": {
                "max_turns": 3,
                "token_budget": {"enabled": False},
            }
        }
    )
    looping = AIMessage(
        content="half an answer",
        tool_calls=[{"name": "noop", "id": "call-x", "args": {}}],
    )
    model = ScriptedChatModel(replies=[looping])
    monkeypatch.setattr(ex, "create_chat_model", lambda **kwargs: model)
    tool = make_tool("noop")
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
            prompt="research",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )

    kwargs = command.update["messages"][0].additional_kwargs
    assert kwargs["subagent_status"] == "completed"
    assert kwargs["subagent_stop_reason"] == "turn_capped"
