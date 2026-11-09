"""M8 §9：``task`` 工具的调度、事件、轮询与返回值。

执行侧（§7 的隔离循环）用 ``fakes/task_tool_harness`` 的替身：工具对它的要求只有
"同步提交 + 能轮询到终态"，所以这里不需要真的事件循环。

三条最容易写错、也最值得钉住的行为：
1. 失败/拒绝**返回工具结果**，不抛异常（抛异常会把整轮 lead 一起带走）；
2. 未知类型 / 白名单拒绝**不启动执行器**，并列出可用类型；
3. 父侧轮询预算耗尽要**先取消、再保留部分结果**。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from langgraph.types import Command

from agentkit.subagents import results
from agentkit.subagents.results import SubagentStatus
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.task_tool_harness import (
    FakeDispatcher,
    FakeRuntime,
    FakeUsageRecorder,
    make_app_config,
    make_tool,
)

TOOL_CALL_ID = "call_1"


@pytest.fixture(autouse=True)
def _clean_registry():
    results.reset_registry()
    yield
    results.reset_registry()


def _tool(
    dispatcher: FakeDispatcher,
    *,
    app_config=None,
    poll_interval: float = 0.005,
    poll_slack_seconds: float = 30.0,
):
    return make_task_tool(
        dispatcher=dispatcher,
        app_config=app_config or make_app_config(),
        poll_interval=poll_interval,
        poll_slack_seconds=poll_slack_seconds,
    )


def _run(
    tool,
    runtime: FakeRuntime,
    *,
    prompt: str = "do the thing",
    subagent_type: str = "general-purpose",
    description: str = "",
    tool_call_id: str = TOOL_CALL_ID,
) -> Command:
    return asyncio.run(
        tool.coroutine(
            runtime=runtime,
            prompt=prompt,
            subagent_type=subagent_type,
            tool_call_id=tool_call_id,
            description=description,
        )
    )


def _tool_message(command: Command):
    return command.update["messages"][0]


def _entry(command: Command) -> dict:
    return command.update["delegations"][0]


def _runtime(**context) -> FakeRuntime:
    return FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1", "user_id": "user-1", **context})


# ------------------------------------------------------------ 拒绝路径（§9.1 第 2、3 步）


def test_unknown_type_fails_and_lists_the_available_types() -> None:
    dispatcher = FakeDispatcher()
    command = _run(
        _tool(dispatcher, app_config=make_app_config(allow_host_bash=True)),
        _runtime(),
        subagent_type="nope",
    )

    assert dispatcher.dispatches == []  # 不许启动执行器
    message = _tool_message(command)
    assert message.status == "error"
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "未知或不允许的子代理类型" in message.content
    assert "可用类型：" in message.content
    assert "general-purpose" in message.content and "bash" in message.content
    assert _entry(command)["status"] == "failed"


def test_a_type_outside_the_whitelist_is_rejected() -> None:
    dispatcher = FakeDispatcher()
    app_config = make_app_config(allow_host_bash=True, custom_agents={"researcher": {"description": "x"}})
    command = _run(
        _tool(dispatcher, app_config=app_config),
        _runtime(allowed_subagents=["bash"]),
        subagent_type="researcher",
    )

    assert dispatcher.dispatches == []
    assert "bash" in _tool_message(command).content
    listed = _tool_message(command).content.split("可用类型：")[-1]
    assert "bash" in listed
    assert "researcher" not in listed


def test_an_empty_whitelist_disables_delegation() -> None:
    dispatcher = FakeDispatcher()
    command = _run(_tool(dispatcher), _runtime(allowed_subagents=[]))

    assert dispatcher.dispatches == []
    assert "本次运行已禁用委派" in _tool_message(command).content


def test_a_malformed_whitelist_fails_closed() -> None:
    dispatcher = FakeDispatcher()
    command = _run(_tool(dispatcher), _runtime(allowed_subagents="all"))

    assert dispatcher.dispatches == []
    assert _tool_message(command).additional_kwargs["subagent_status"] == "failed"


def test_an_unresolvable_model_fails_the_delegation_without_dispatching() -> None:
    dispatcher = FakeDispatcher()
    tool = _tool(dispatcher, app_config=make_app_config(models=[]))
    command = _run(tool, _runtime())

    assert dispatcher.dispatches == []
    assert "无法解析子代理模型" in _tool_message(command).content
    assert _entry(command)["status"] == "failed"


# ------------------------------------------------------------ 正常路径（§9.1 第 4–9 步）


def test_the_happy_path_emits_start_step_and_terminal_events() -> None:
    dispatcher = FakeDispatcher(steps=({"message_index": 0, "message": {"type": "ai"}},))
    runtime = _runtime()
    command = _run(_tool(dispatcher), runtime, description="scan the repo")

    assert runtime.event_types() == ["task_started", "task_running", "task_completed"]
    started, running, completed = runtime.events
    assert started["task_id"] == TOOL_CALL_ID and started["description"] == "scan the repo"
    assert started["model_name"] == "deepseek-reasoner"
    assert running["message_index"] == 0 and running["task_id"] == TOOL_CALL_ID
    assert completed["status"] == "completed" and completed["task_id"] == TOOL_CALL_ID

    message = _tool_message(command)
    assert message.status == "success"
    assert message.content == "任务成功。\n结果：the answer"
    assert message.additional_kwargs["subagent_status"] == "completed"
    assert message.additional_kwargs["subagent_model_name"] == "deepseek-reasoner"
    assert message.additional_kwargs["subagent_result_brief"] == "the answer"


def test_cited_sources_are_carried_on_the_task_result() -> None:
    citation = {
        "citation_id": "k1234abcd",
        "chunk_id": "d1:0",
        "document_id": "d1",
        "knowledge_base_id": "b1",
        "knowledge_base_name": "产品文档",
        "knowledge_base_type": "technical",
        "filename": "manual.md",
        "section": "安装",
        "page": 2,
        "chunk_index": 0,
        "quote": "安装步骤",
    }
    dispatcher = FakeDispatcher(citations=(citation,))

    command = _run(_tool(dispatcher), _runtime())

    assert _tool_message(command).additional_kwargs["knowledge_citations"] == [citation]


def test_the_command_writes_one_ledger_entry_next_to_the_tool_message() -> None:
    dispatcher = FakeDispatcher()
    command = _run(_tool(dispatcher), _runtime(), prompt="line one\nline two")

    entry = _entry(command)
    assert entry["id"] == TOOL_CALL_ID
    assert entry["status"] == "completed"
    assert entry["subagent_type"] == "general-purpose"
    assert entry["run_id"] == "run-1"
    assert entry["description"] == "line one"
    assert entry["result_brief"] == "the answer"


def test_the_tool_call_id_is_the_external_key_only() -> None:
    """§2：台账 / SSE 用 provider 的 tool_call_id；注册表主键是服务端 uuid。"""
    dispatcher = FakeDispatcher()
    _run(_tool(dispatcher), _runtime())

    holder = dispatcher.dispatch.holder
    assert holder.external_task_id == TOOL_CALL_ID
    assert holder.task_id != TOOL_CALL_ID
    assert len(holder.task_id) == 32


def test_the_dispatch_carries_everything_the_executor_needs() -> None:
    dispatcher = FakeDispatcher()
    lead_tools = [make_tool("read_file")]
    runtime = _runtime()
    runtime.tools = lead_tools
    runtime.state = {"uploaded_files": [{"filename": "a.txt"}]}
    runtime.config = {"configurable": {"model_name": "deepseek-reasoner"}}
    _run(_tool(dispatcher), runtime, description="scan")

    dispatch = dispatcher.dispatch
    assert dispatch.config.name == "general-purpose"
    assert dispatch.config.max_turns == 150
    assert dispatch.model_name == "deepseek-reasoner"
    assert dispatch.label == "scan"
    assert (dispatch.run_id, dispatch.thread_id, dispatch.user_id) == ("run-1", "thread-1", "user-1")
    assert dispatch.parent_model == "deepseek-reasoner"
    assert dispatch.allowed_subagents is None
    assert dispatch.uploaded_files == [{"filename": "a.txt"}]
    assert dispatch.tools == lead_tools
    assert dispatch.holder.status.is_terminal


def test_the_label_falls_back_to_the_first_line_of_the_prompt() -> None:
    dispatcher = FakeDispatcher()
    _run(_tool(dispatcher), _runtime(), prompt="first line\nsecond line")
    assert dispatcher.dispatch.label == "first line"


def test_without_run_id_the_delegation_still_works_but_warns(caplog: pytest.LogCaptureFixture) -> None:
    dispatcher = FakeDispatcher()
    runtime = FakeRuntime(context={})
    with caplog.at_level(logging.WARNING, logger="agentkit.tools.builtin.task_tool"):
        command = _run(_tool(dispatcher), runtime)

    assert dispatcher.dispatch.run_id is None
    assert "run_id" not in _entry(command)
    assert any("without run_id" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------------ 上传快照（§9.1 第 4 步）


def test_the_upload_snapshot_is_deep_copied() -> None:
    dispatcher = FakeDispatcher()
    state = {"uploaded_files": [{"filename": "a.txt", "outline": [{"title": "T"}]}]}
    runtime = _runtime()
    runtime.state = state

    asyncio.run(
        _tool(dispatcher).coroutine(
            runtime=runtime,
            prompt="x",
            subagent_type="general-purpose",
            tool_call_id=TOOL_CALL_ID,
        )
    )
    state["uploaded_files"][0]["outline"][0]["title"] = "changed"
    state["uploaded_files"].append({"filename": "b.txt"})

    assert dispatcher.dispatch.uploaded_files == [{"filename": "a.txt", "outline": [{"title": "T"}]}]


def test_an_empty_upload_list_is_a_valid_snapshot() -> None:
    dispatcher = FakeDispatcher()
    runtime = _runtime()
    runtime.state = {"uploaded_files": []}
    _run(_tool(dispatcher), runtime)
    assert dispatcher.dispatch.uploaded_files == []


@pytest.mark.parametrize("bad", [[{"filename": 1}], "not-a-list", [None]])
def test_a_malformed_upload_snapshot_is_disabled(bad: object) -> None:
    """缺失或畸形 → 子代理不许看到上传列表（fail closed）。"""
    dispatcher = FakeDispatcher()
    runtime = _runtime()
    runtime.state = {"uploaded_files": bad}
    _run(_tool(dispatcher), runtime)
    assert dispatcher.dispatch.uploaded_files is None


def test_a_missing_upload_snapshot_is_disabled() -> None:
    dispatcher = FakeDispatcher()
    _run(_tool(dispatcher), _runtime())
    assert dispatcher.dispatch.uploaded_files is None


# ------------------------------------------------------------ 终态与失败（§9.2 / §13）


@pytest.mark.parametrize(
    ("terminal", "external", "event"),
    [
        (SubagentStatus.COMPLETED, "completed", "task_completed"),
        (SubagentStatus.FAILED, "failed", "task_failed"),
        (SubagentStatus.CANCELLED, "cancelled", "task_cancelled"),
        (SubagentStatus.TIMED_OUT, "timed_out", "task_timed_out"),
    ],
)
def test_every_terminal_status_maps_to_a_contract_value(
    terminal: SubagentStatus, external: str, event: str
) -> None:
    dispatcher = FakeDispatcher(terminal=terminal, result="partial", error="why")
    runtime = _runtime()
    command = _run(_tool(dispatcher), runtime)

    assert _tool_message(command).additional_kwargs["subagent_status"] == external
    assert _entry(command)["status"] == external
    assert runtime.event_types()[-1] == event


def test_a_failed_delegation_is_a_tool_result_not_an_exception() -> None:
    dispatcher = FakeDispatcher(terminal=SubagentStatus.FAILED, result=None, error="provider blew up")
    command = _run(_tool(dispatcher), _runtime())

    message = _tool_message(command)
    assert message.status == "error"
    assert "任务失败。" in message.content and "provider blew up" in message.content
    assert message.additional_kwargs["subagent_error"] == "provider blew up"


def test_cancellation_keeps_the_partial_result() -> None:
    """§17.1：点停止之后已经拿到的结论要留着——取消只停止后续动作。"""
    dispatcher = FakeDispatcher(terminal=SubagentStatus.CANCELLED, result="half an answer")
    command = _run(_tool(dispatcher), _runtime())

    assert "half an answer" in _tool_message(command).content
    assert _entry(command)["status"] == "cancelled"


def test_a_capped_completion_is_still_a_success_with_a_stop_reason() -> None:
    """§13.1：到上限**不是失败**，但也不是做完了。"""
    dispatcher = FakeDispatcher(terminal=SubagentStatus.COMPLETED, result="half", stop_reason="turn_capped")
    command = _run(_tool(dispatcher), _runtime())

    message = _tool_message(command)
    assert message.status == "success"
    assert "轮次预算" in message.content
    assert message.additional_kwargs["subagent_stop_reason"] == "turn_capped"
    assert _entry(command)["stop_reason"] == "turn_capped"


# ------------------------------------------------------------ 轮询（§9.1 第 8 步 / §17）


def test_steps_captured_while_polling_are_reported_once_each() -> None:
    dispatcher = FakeDispatcher(
        delay=0.05, late_steps=({"message_index": 1, "message": {"type": "tool"}},)
    )
    runtime = _runtime()
    _run(_tool(dispatcher), runtime)

    running = [event for event in runtime.events if event["type"] == "task_running"]
    assert len(running) == 1
    assert running[0]["message_index"] == 1
    assert runtime.events[-1]["type"] == "task_completed"


def test_polling_budget_exhaustion_cancels_and_keeps_partial_results() -> None:
    dispatcher = FakeDispatcher(terminal=None, result="partial")
    runtime = _runtime()
    # 把轮询预算压到已过期：不必真等 1800 秒（timeout_seconds + slack <= 0）
    command = _run(_tool(dispatcher, poll_slack_seconds=-1800), runtime)

    assert dispatcher.dispatch.holder.cancel_event.is_set(), "父侧放弃时要真的喊停子代理"
    assert _tool_message(command).additional_kwargs["subagent_status"] == "polling_timed_out"
    assert _entry(command)["status"] == "polling_timed_out"
    assert runtime.event_types()[-1] == "task_timed_out"


def test_the_registry_entry_is_dropped_after_a_terminal_delegation() -> None:
    dispatcher = FakeDispatcher()
    _run(_tool(dispatcher), _runtime())
    assert results.get(dispatcher.dispatch.holder.task_id) is None
    assert results.cancel_all_for_run("run-1") == []


# ------------------------------------------------------------ 提交与事件失败（§17）


def test_a_submission_failure_fails_the_delegation_and_cleans_up() -> None:
    dispatcher = FakeDispatcher(fail_start=RuntimeError("loop is gone"))
    command = _run(_tool(dispatcher), _runtime())

    message = _tool_message(command)
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "子代理运行时不可用" in message.content
    holder = dispatcher.dispatch.holder
    assert holder.status is SubagentStatus.FAILED
    assert results.get(holder.task_id) is None


def test_a_failed_started_event_cancels_and_cleans_up() -> None:
    """§17：``task_started`` 是真正开跑前的 await 点，漏了清理就留下永远 RUNNING 的条目。"""
    dispatcher = FakeDispatcher()
    runtime = _runtime()
    runtime.stream_error = RuntimeError("writer is gone")
    command = _run(_tool(dispatcher), runtime)

    assert "无法上报子代理已启动" in _tool_message(command).content
    holder = dispatcher.dispatch.holder
    assert holder.cancel_event.is_set()
    assert results.get(holder.task_id) is None


def test_later_event_failures_do_not_fail_the_delegation() -> None:
    """§3.2 第 6 步：事件发失败只影响卡片好看程度，**不影响结果**。"""
    dispatcher = FakeDispatcher(steps=({"message_index": 0, "message": {"type": "ai"}},))
    runtime = _runtime()
    runtime.fail_events_after = 1  # task_started 发得出去，后面全炸
    command = _run(_tool(dispatcher), runtime)

    assert _tool_message(command).additional_kwargs["subagent_status"] == "completed"
    assert runtime.event_types() == ["task_started"]


# ------------------------------------------------------------ 用量归账（§15）


def test_usage_records_go_to_the_parent_run_journal() -> None:
    dispatcher = FakeDispatcher(usage=({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},))
    recorder = FakeUsageRecorder()
    runtime = _runtime()
    runtime.config = {"callbacks": [recorder, object()]}
    _run(_tool(dispatcher), runtime)

    assert recorder.calls == [[{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}]]


def test_usage_is_also_visible_on_the_tool_message() -> None:
    dispatcher = FakeDispatcher(usage=({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},))
    command = _run(_tool(dispatcher), _runtime())

    assert _tool_message(command).additional_kwargs["subagent_token_usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


def test_a_missing_usage_recorder_is_harmless() -> None:
    dispatcher = FakeDispatcher(usage=({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},))
    command = _run(_tool(dispatcher), _runtime())
    assert _tool_message(command).additional_kwargs["subagent_status"] == "completed"


def test_zero_usage_is_omitted_rather_than_zeroed() -> None:
    dispatcher = FakeDispatcher(usage=({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},))
    command = _run(_tool(dispatcher), _runtime())
    assert "subagent_token_usage" not in _tool_message(command).additional_kwargs
