"""M8 §13.2：图干净地结束 ≠ 子代理成功，而**只有标记是权威的**。

M6 的 LLM 错误中间件会**故意**把 provider 异常转成一条 AIMessage，让图能干净地结束，
并盖上 ``additional_kwargs.myflow_error_fallback = true``。执行器终态化时检查最后一条
assistant 消息上的这个标记，有就映射为 ``failed``。

反面同样重要：**看起来像错误的散文没有标记就是正常完成**——执行器和前端都不许把展示
文本当状态协议解析。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import ScriptedChatModel
from fakes.task_tool_harness import FakeRuntime, make_app_config, make_tool


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    yield
    results.reset_registry()
    ex.shutdown_isolated_subagent_loop()


def _run(*, tools: list | None = None):
    app_config = make_app_config()
    task_tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config),
        app_config=app_config,
        poll_interval=0.005,
    )
    runtime = FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1"})
    runtime.tools = tools or []
    command = asyncio.run(
        task_tool.coroutine(
            runtime=runtime,
            prompt="do it",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )
    return command.update["messages"][0]


@pytest.fixture
def scripted(monkeypatch):
    def _install(reply: AIMessage):
        monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [])
        monkeypatch.setattr(ex, "create_chat_model", lambda **kw: ScriptedChatModel(replies=[reply]))

    return _install


def test_the_fallback_marker_maps_to_failed(scripted) -> None:
    """那条消息读起来像个正常回答——但标记说它是降级产物。"""
    scripted(
        AIMessage(
            content="I could not reach the model, here is a friendly sentence instead.",
            additional_kwargs={"myflow_error_fallback": True},
        )
    )

    message = _run()

    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "模型调用失败" in message.additional_kwargs["subagent_error"]
    assert message.status == "error"


def test_prose_that_looks_like_an_error_is_a_normal_completion(scripted) -> None:
    """§13.2：没有标记 → 正常完成。散文不是状态协议。"""
    scripted(AIMessage(content="Error: the log had 3 error lines, here is the summary..."))

    message = _run()

    assert message.additional_kwargs["subagent_status"] == "completed"
    assert "subagent_error" not in message.additional_kwargs
    assert "Error: the log had 3 error lines" in message.content


def test_an_empty_answer_without_a_marker_is_a_failure(scripted) -> None:
    scripted(AIMessage(content="   "))

    message = _run()

    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "没有产出结论" in message.additional_kwargs["subagent_error"]


def test_the_marker_only_counts_on_the_last_assistant_message(monkeypatch) -> None:
    """中间某一步降级过、但最后一步是干净的结论：这次委派就是完成的。"""
    monkeypatch.setattr(ex, "build_subagent_runtime_middlewares", lambda *a, **kw: [])
    monkeypatch.setattr(
        ex,
        "create_chat_model",
        lambda **kw: ScriptedChatModel(
            replies=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "noop", "id": "call-x", "args": {}}],
                    additional_kwargs={"myflow_error_fallback": True},
                ),
                AIMessage(content="recovered answer"),
            ]
        ),
    )

    message = _run(tools=[make_tool("noop")])

    assert message.additional_kwargs["subagent_status"] == "completed"
    assert "recovered answer" in message.content
