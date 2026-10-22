"""M8 §2 / §8：两个 ID 的纪律。

``execution_id``（服务端 uuid4）是注册表主键；``tool_call_id``（provider 给的）只是对外
相关键。**合并这两者**在高并发下会出现"别人的调研任务莫名其妙变成已取消"，而日志里
看不出任何异常——只因为两个 run 撞上了同一个 provider id。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest

from agentkit.subagents import results
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.task_tool_harness import FakeDispatcher, FakeRuntime, make_app_config

TOOL_CALL_ID = "call_1"


@pytest.fixture(autouse=True)
def _clean_registry():
    results.reset_registry()
    yield
    results.reset_registry()


def _holder(execution_id: str, *, external: str = TOOL_CALL_ID) -> SubagentResult:
    return SubagentResult(
        task_id=execution_id,
        trace_id="trace",
        status=SubagentStatus.RUNNING,
        external_task_id=external,
    )


def test_the_same_tool_call_id_in_two_runs_gets_two_holders() -> None:
    first = _holder("exec-a")
    second = _holder("exec-b")
    results.register(first, run_id="run-a")
    results.register(second, run_id="run-b")

    assert results.get("exec-a") is first
    assert results.get("exec-b") is second
    assert first.external_task_id == second.external_task_id == TOOL_CALL_ID


def test_cancelling_one_run_leaves_the_other_alone() -> None:
    first = _holder("exec-a")
    second = _holder("exec-b")
    results.register(first, run_id="run-a")
    results.register(second, run_id="run-b")

    assert results.cancel_all_for_run("run-a") == ["exec-a"]
    assert first.cancel_event.is_set()
    assert not second.cancel_event.is_set(), "另一个 run 的子代理不许被误伤"


def test_cancel_all_refuses_to_guess_without_a_run_id() -> None:
    holder = _holder("exec-a")
    results.register(holder, run_id="run-a")

    assert results.cancel_all_for_run(None) == []
    assert not holder.cancel_event.is_set()


def test_cleanup_is_idempotent_and_cancel_becomes_a_no_op() -> None:
    holder = _holder("exec-a")
    results.register(holder, run_id="run-a")

    results.cleanup("exec-a")
    results.cleanup("exec-a")
    assert results.get("exec-a") is None
    assert results.request_cancel("exec-a") is False
    assert results.cancel_all_for_run("run-a") == []


def test_request_cancel_cancels_the_submitted_future_too() -> None:
    """§8：协作式 ``cancel_event`` 之外，还要把已提交的 future 取消掉（锁外做）。"""
    holder = _holder("exec-a")
    results.register(holder, run_id="run-a")
    future: Future = Future()
    results.attach_future("exec-a", future)

    assert results.request_cancel("exec-a") is True
    assert holder.cancel_event.is_set()
    assert future.cancelled()


def test_cancel_all_for_run_only_touches_its_own_futures() -> None:
    first, second = _holder("exec-a"), _holder("exec-b")
    results.register(first, run_id="run-a")
    results.register(second, run_id="run-b")
    first_future: Future = Future()
    second_future: Future = Future()
    results.attach_future("exec-a", first_future)
    results.attach_future("exec-b", second_future)

    results.cancel_all_for_run("run-a")

    assert first_future.cancelled()
    assert not second_future.cancelled() and not second_future.done()


def test_attaching_a_future_after_cleanup_is_a_no_op() -> None:
    """条目已经摘掉之后再登记 future 只会泄漏内存——所以直接忽略。"""
    holder = _holder("exec-a")
    results.register(holder, run_id="run-a")
    results.cleanup("exec-a")

    results.attach_future("exec-a", Future())
    assert results.get("exec-a") is None


def test_the_first_terminal_write_wins() -> None:
    """后台超时、父 run 取消、worker 正常结束会竞态写同一个 holder。"""
    holder = _holder("exec-a")

    assert holder.try_set_terminal(SubagentStatus.COMPLETED, result="the answer") is True
    assert holder.try_set_terminal(SubagentStatus.FAILED, error="late timeout") is False
    assert holder.status is SubagentStatus.COMPLETED
    assert holder.result == "the answer"
    assert holder.error is None


def test_a_non_terminal_write_is_a_programming_error() -> None:
    holder = _holder("exec-a")
    with pytest.raises(ValueError, match="terminal status"):
        holder.try_set_terminal(SubagentStatus.RUNNING)
    with pytest.raises(ValueError, match="unknown terminal payload"):
        holder.try_set_terminal(SubagentStatus.COMPLETED, brief="typo")


def test_usage_updates_stop_after_the_terminal_write() -> None:
    holder = _holder("exec-a")
    holder.update_token_usage_records([{"total_tokens": 5}])
    holder.try_set_terminal(SubagentStatus.COMPLETED, result="done")
    holder.update_token_usage_records([{"total_tokens": 99}])

    assert holder.token_usage_records == [{"total_tokens": 5}]


def test_usage_updates_replace_the_snapshot_instead_of_appending() -> None:
    """一次 LLM 回调可能打多次 on_llm_end：追加会把同一次调用记两遍。"""
    holder = _holder("exec-a")
    holder.update_token_usage_records([{"total_tokens": 5}])
    holder.update_token_usage_records([{"total_tokens": 7}])
    assert holder.token_usage_records == [{"total_tokens": 7}]


def test_two_runs_with_the_same_tool_call_id_delegate_independently() -> None:
    """并发跑两个 ``task`` 调用，provider 给的 id 一样——两边各拿各的结论。"""
    first_dispatcher = FakeDispatcher(result="A", delay=0.02)
    second_dispatcher = FakeDispatcher(result="B", delay=0.02)
    tool = make_task_tool(
        dispatcher=first_dispatcher,
        app_config=make_app_config(),
        poll_interval=0.005,
    )
    other_tool = make_task_tool(
        dispatcher=second_dispatcher,
        app_config=make_app_config(),
        poll_interval=0.005,
    )

    async def run_both():
        return await asyncio.gather(
            tool.coroutine(
                runtime=FakeRuntime(context={"run_id": "run-a"}),
                prompt="A",
                subagent_type="general-purpose",
                tool_call_id=TOOL_CALL_ID,
            ),
            other_tool.coroutine(
                runtime=FakeRuntime(context={"run_id": "run-b"}),
                prompt="B",
                subagent_type="general-purpose",
                tool_call_id=TOOL_CALL_ID,
            ),
        )

    first, second = asyncio.run(run_both())

    assert first.update["messages"][0].content == "任务成功。\n结果：A"
    assert second.update["messages"][0].content == "任务成功。\n结果：B"
    assert first.update["delegations"][0]["run_id"] == "run-a"
    assert second.update["delegations"][0]["run_id"] == "run-b"
    assert first_dispatcher.dispatch.holder.task_id != second_dispatcher.dispatch.holder.task_id


def test_the_delegation_survives_someone_else_cleaning_the_registry() -> None:
    """§2 推论一：闭包持有**自己的** ``SubagentResult``，不回注册表反查所有权。"""

    class CleaningDispatcher(FakeDispatcher):
        def start(self, dispatch) -> None:
            super().start(dispatch)
            results.cleanup(dispatch.holder.task_id)  # 别人先摘了条目

    dispatcher = CleaningDispatcher(result="still here")
    command = asyncio.run(
        make_task_tool(dispatcher=dispatcher, app_config=make_app_config(), poll_interval=0.005)
        .coroutine(
            runtime=FakeRuntime(context={"run_id": "run-1"}),
            prompt="x",
            subagent_type="general-purpose",
            tool_call_id=TOOL_CALL_ID,
        )
    )

    assert "still here" in command.update["messages"][0].content
    assert command.update["delegations"][0]["status"] == "completed"
