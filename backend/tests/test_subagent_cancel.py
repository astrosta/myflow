"""M8 §17：取消、超时与父 run 收尾。

§17 的四行矩阵里，执行侧的取消检查（chunk 边界）与墙钟超时在 §11/§13 已经钉过；这里管的是
**父侧**那三件事：

1. 父 run 取消 → `cancel_all_for_run` 扇出到每个 holder（只碰这次 run 的）；
2. 父 run 收尾 → worker 的 `finally` 亲自取消并**等**隔离循环清理完（有超时）——不能假设
   `task` 的轮询循环还活着：客户端断开时 ToolNode 可能已经把它取消了；
3. 取消路径上的 `task` 工具 → 已经拿到的结论与用量照常交付，然后原样上抛取消。

最后一条是 §17.1 那两句承诺的落点：**取消不退钱，但要算清楚**、**取消之后结论还在**。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from contextlib import suppress
from types import SimpleNamespace

import pytest

from agentkit.config import AppConfig, SubagentRuntimeConfig
from agentkit.runtime.runs import worker as worker_module
from agentkit.subagents import executor as executor_module
from agentkit.subagents import results as registry
from agentkit.subagents.capacity import SubagentExecutionCapacity
from agentkit.subagents.config import SubagentConfig
from agentkit.subagents.results import (
    SubagentResult,
    SubagentStatus,
    attach_future,
    cancel_all_for_run,
    cleanup,
    future_for,
    holders_for_run,
    register,
    request_cancel,
    wait_for_run_cleanup,
)
from agentkit.subagents.results import _registry_lock
from agentkit.tools.builtin.task_tool import _abandon_cancelled, make_task_tool
from fakes.task_tool_harness import (
    FakeDispatcher,
    FakeRuntime,
    FakeUsageRecorder,
    make_app_config,
    make_tool,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset_registry()
    yield
    registry.reset_registry()
    executor_module.shutdown_isolated_subagent_loop()


def _holder(task_id: str = "exec-1", *, status: SubagentStatus = SubagentStatus.RUNNING):
    return SubagentResult(task_id=task_id, trace_id="trace-1", status=status)


class _FakeFuture:
    """只记账的执行 future。三种结局各要一条断言（§17 的"等得到 / 等不到"）。"""

    #: ok = 正常结束；cancelled = 取消后它就结束了；stuck = 取消也没用（隔离循环还在忙）
    def __init__(self, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.cancelled = False
        self.waited = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.outcome == "ok":
            self.outcome = "cancelled"

    def result(self, timeout: float | None = None):
        self.waited = True
        if self.outcome == "stuck":
            raise concurrent.futures.TimeoutError()
        if self.outcome == "cancelled":
            raise concurrent.futures.CancelledError()
        return None


# ---------------------------------------------------------------- §17 第 1 行：扇出


def test_cancel_all_for_run_only_touches_that_run() -> None:
    """同一个进程里可能还有别的 run 的子代理在跑，扇出必须按 run_id 限定。"""
    mine = _holder("exec-1")
    theirs = _holder("exec-2")
    register(mine, run_id="r1")
    register(theirs, run_id="r2")

    assert cancel_all_for_run("r1") == ["exec-1"]

    assert mine.cancel_event.is_set()
    assert not theirs.cancel_event.is_set()


def test_cancel_all_for_run_refuses_without_a_run_id(caplog) -> None:
    """拿不到 run_id 时宁可什么都不取消——猜一个「全都取消」会误伤别人的委派。"""
    holder = _holder()
    register(holder, run_id="r1")

    with caplog.at_level(logging.WARNING, logger="agentkit.subagents.results"):
        assert cancel_all_for_run(None) == []

    assert not holder.cancel_event.is_set()
    assert "without a run_id" in caplog.text


def test_a_cleaned_up_entry_is_not_cancelled_by_the_fanout() -> None:
    """终态之后条目已经摘掉：收尾再扇出一次不许把它算进来（重复取消会让计数漂走）。"""
    holder = _holder()
    register(holder, run_id="r1")
    cleanup(holder.task_id)

    assert cancel_all_for_run("r1") == []
    assert not holder.cancel_event.is_set()


def test_the_future_is_cancelled_outside_the_registry_lock() -> None:
    """§8：`future.cancel()` 可能同步触发完成回调，而回调会再拿同一把锁——在锁里调就是死锁。"""
    holder = _holder()
    register(holder, run_id="r1")

    class _ProbingFuture(_FakeFuture):
        acquired: bool | None = None

        def cancel(self) -> None:
            # 有界探测：调用方还攥着锁的话，这里 0.5 秒后返回 False，而不是把测试挂死
            _ProbingFuture.acquired = _registry_lock.acquire(timeout=0.5)
            if _ProbingFuture.acquired:
                _registry_lock.release()
            super().cancel()

    future = _ProbingFuture()
    attach_future(holder.task_id, future)

    assert request_cancel(holder.task_id) is True

    assert future.cancelled is True
    assert holder.cancel_event.is_set()
    assert _ProbingFuture.acquired is True, "future.cancel() 必须在注册表锁之外调"


def test_request_cancel_on_an_unknown_id_is_false() -> None:
    assert request_cancel("nope") is False
    assert future_for("nope") is None


# ---------------------------------------------------------------- §17 + §10.2：槽位


def test_cancelling_a_running_subagent_releases_its_slot() -> None:
    """取消释放槽位：shield 过的那次 release 就是为这条路径写的（§10.2）。"""

    async def scenario() -> tuple[int, int]:
        capacity = SubagentExecutionCapacity(SubagentRuntimeConfig(max_running=1))
        started = asyncio.Event()

        async def hold() -> None:
            async with capacity.slot():
                started.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(hold())
        await started.wait()
        running_before = capacity.snapshot().running
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return running_before, capacity.snapshot().running

    assert asyncio.run(scenario()) == (1, 0)


def test_cancelling_a_queued_subagent_leaves_the_queue() -> None:
    """排队中的取消必须把队列位置一起交回去，否则队列会随每次取消慢慢变短。"""

    async def scenario() -> tuple[int, int, int]:
        capacity = SubagentExecutionCapacity(SubagentRuntimeConfig(max_running=1))
        started = asyncio.Event()

        async def hold() -> None:
            async with capacity.slot():
                started.set()
                await asyncio.sleep(30)

        first = asyncio.create_task(hold())
        await started.wait()
        waiter = asyncio.create_task(hold())
        await asyncio.sleep(0.05)  # 让它排进队列（第一次 sleep 是不会让出到队列的）
        queued_before = capacity.snapshot().queued
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        snapshot = capacity.snapshot()
        first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        return queued_before, snapshot.queued, snapshot.running

    assert asyncio.run(scenario()) == (1, 0, 1)


# ---------------------------------------------------------------- §17：父 run 收尾


def test_wait_for_run_cleanup_counts_settled_and_pending() -> None:
    done = _holder("exec-1")
    cancelled = _holder("exec-2")
    stuck = _holder("exec-3")
    for holder in (done, cancelled, stuck):
        register(holder, run_id="r1")
    cancelled_future = _FakeFuture()
    cancelled_future.cancel()
    attach_future(done.task_id, _FakeFuture())
    attach_future(cancelled.task_id, cancelled_future)
    attach_future(stuck.task_id, _FakeFuture("stuck"))

    settled, pending = wait_for_run_cleanup("r1", timeout=0.05)

    assert (settled, pending) == (2, 1), "取消 / 失败都算收尾完成，只有真卡住的才算 pending"


def test_the_worker_finalizer_cancels_and_waits() -> None:
    """§17：父 run 收尾时 worker 亲自取消，并等隔离循环把清理跑完。"""
    holder = _holder()
    future = _FakeFuture()
    register(holder, run_id="r1")
    attach_future(holder.task_id, future)

    asyncio.run(worker_module._finalize_subagents("r1"))

    assert holder.cancel_event.is_set()
    assert future.cancelled is True
    assert future.waited is True, "只取消不等，沙箱排空 / 租约释放可能拖到 run 结束之后"


def test_the_worker_finalizer_does_not_wait_when_nothing_is_registered() -> None:
    """正常收尾（子代理早就终态并摘掉）不该有任何开销，也不该挂住。"""
    asyncio.run(worker_module._finalize_subagents("r1"))
    assert holders_for_run("r1") == []


def test_the_worker_finalizer_gives_up_on_a_stuck_subagent(monkeypatch, caplog) -> None:
    """清理等不到也不能挡住 run 的终态：打一条 warning，然后继续收尾。"""
    monkeypatch.setattr(worker_module, "SUBAGENT_CLEANUP_TIMEOUT_SECONDS", 0.05)
    holder = _holder()
    register(holder, run_id="r1")
    attach_future(holder.task_id, _FakeFuture("stuck"))

    with caplog.at_level(logging.WARNING, logger="agentkit.runtime.runs.worker"):
        asyncio.run(worker_module._finalize_subagents("r1"))

    assert "still running" in caplog.text


# ---------------------------------------------------------------- §17：取消路径上的 task 工具


def _runtime(**kwargs) -> FakeRuntime:
    runtime = FakeRuntime(context={"run_id": "r1", "thread_id": "t1"}, **kwargs)
    runtime.tools = [make_tool("read_file")]
    return runtime


def _tool(dispatcher) -> object:
    return make_task_tool(
        dispatcher=dispatcher,
        app_config=make_app_config(allow_host_bash=True),
        poll_interval=0.01,
    )


USAGE_RECORD = {
    "input_tokens": 4,
    "output_tokens": 6,
    "total_tokens": 10,
    "caller": "subagent:general-purpose",
}


def test_a_cancelled_tool_reports_usage_and_a_terminal_event() -> None:
    """§17.1：取消不退钱，但要算清楚——用量照常上报，卡片拿到一个明确的终态。"""
    dispatcher = FakeDispatcher(terminal=None, usage=(USAGE_RECORD,))
    recorder = FakeUsageRecorder()
    runtime = _runtime()
    tool = _tool(dispatcher)

    async def scenario() -> None:
        call = asyncio.create_task(
            tool.coroutine(
                runtime=runtime,
                prompt="go",
                subagent_type="general-purpose",
                tool_call_id="call_1",
            )
        )
        await asyncio.sleep(0.05)  # 让它进到轮询里
        # 记录器是提交那一刻解析好的（§7.5）；替身里手工补上
        dispatcher.dispatch.usage_recorder = recorder
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())

    holder = dispatcher.dispatch.holder
    assert holder.cancel_event.is_set(), "父侧取消要广播到执行侧"
    assert holder.status is SubagentStatus.CANCELLED
    assert runtime.event_types()[-1] == "task_cancelled"
    assert runtime.events[-1]["usage"]["total_tokens"] == 10
    assert recorder.calls and recorder.calls[0][0]["input_tokens"] == 4
    assert registry.get(holder.task_id) is None, "取消路径也要把注册表条目摘掉"


def test_a_cancelled_tool_keeps_the_result_the_executor_already_captured() -> None:
    """§17.1：取消只停止后续动作，**不回收已经拿到的结论**。"""

    class _SlowDispatcher:
        """收到父侧取消之后才落终态，并带上已经拿到的那点结论。"""

        def __init__(self) -> None:
            self.dispatch = None
            self.done = concurrent.futures.Future()

        def start(self, dispatch) -> None:
            self.dispatch = dispatch
            holder = dispatch.holder
            attach_future(holder.task_id, self.done)

            def _finish() -> None:
                holder.cancel_event.wait(timeout=5)
                holder.try_set_terminal(SubagentStatus.CANCELLED, result="PARTIAL-CONCLUSION")
                if not self.done.done():  # 父侧可能已经把 future 取消掉了
                    self.done.set_result(None)

            threading.Thread(target=_finish, daemon=True).start()

    dispatcher = _SlowDispatcher()
    runtime = _runtime()
    tool = _tool(dispatcher)

    async def scenario() -> None:
        call = asyncio.create_task(
            tool.coroutine(
                runtime=runtime,
                prompt="go",
                subagent_type="general-purpose",
                tool_call_id="call_1",
            )
        )
        await asyncio.sleep(0.05)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())

    holder = dispatcher.dispatch.holder
    assert holder.status is SubagentStatus.CANCELLED
    assert holder.result == "PARTIAL-CONCLUSION"
    assert runtime.events[-1]["result"] == "PARTIAL-CONCLUSION", "终态事件带着已经拿到的结论"


def test_the_cancel_path_never_rewrites_a_finished_delegation() -> None:
    """状态只认标记字段：已经 completed 的委派，晚到的取消不许把它改成 cancelled。"""
    holder = _holder()
    holder.try_set_terminal(SubagentStatus.COMPLETED, result="DONE")
    dispatcher = SimpleNamespace(holder=holder, usage_recorder=None)
    runtime = _runtime()

    asyncio.run(
        _abandon_cancelled(
            holder,
            runtime,
            dispatch=dispatcher,
            task_id="call_1",
            label="look around",
            subagent_type="general-purpose",
            model_name="deepseek-reasoner",
        )
    )

    assert holder.status is SubagentStatus.COMPLETED
    assert holder.result == "DONE"
    assert runtime.event_types()[-1] == "task_completed"


def test_the_cancel_path_marks_a_never_finishing_subagent_cancelled() -> None:
    """执行侧没能落终态时，父侧如实标 cancelled——宁可没有部分结果，也不留一张转圈的卡片。"""
    holder = _holder()
    dispatcher = SimpleNamespace(holder=holder, usage_recorder=None)
    runtime = _runtime()

    asyncio.run(
        _abandon_cancelled(
            holder,
            runtime,
            dispatch=dispatcher,
            task_id="call_1",
            label="look around",
            subagent_type="bash",
            model_name="deepseek-reasoner",
        )
    )

    assert holder.status is SubagentStatus.CANCELLED
    assert runtime.event_types()[-1] == "task_cancelled"
    assert runtime.events[-1]["task_id"] == "call_1", "对外只暴露 tool_call_id（§2）"


# ---------------------------------------------------------------- §17 + §10：三个长任务一起收


def test_the_finalizer_reclaims_three_running_subagents(monkeypatch) -> None:
    """§18 的压力形状（去掉秒表）：3 个卡在模型调用里的子代理，父 run 收尾时全部收回。

    走真执行器 + 隔离循环：取消 → 隔离循环在 await 点收到 CancelledError → 终态化 → 释放槽位。
    槽位计数归零是「没有残留」的可观测证据（沙箱租约的同类断言在 `test_subagent_sandbox.py`）。
    """
    from agentkit.subagents import executor as subagents
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from fakes.chat_model import FakeChatModel

    started = threading.Semaphore(0)

    class _BlockingModel(FakeChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            started.release()
            await asyncio.sleep(30)  # 父侧取消会让这个 await 直接抛 CancelledError
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="never"))])

    app_config = AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )
    monkeypatch.setattr(subagents, "create_chat_model", lambda **kwargs: _BlockingModel())
    monkeypatch.setattr(subagents, "build_subagent_runtime_middlewares", lambda *a, **kw: [])
    capacity = SubagentExecutionCapacity(SubagentRuntimeConfig(max_running=3))
    executor = executor_module.SubagentExecutor(app_config=app_config, capacity=capacity)

    dispatches = []
    for index in range(3):
        holder = _holder(f"exec-{index}")
        holder.external_task_id = f"call_{index}"
        dispatch = SimpleNamespace(
            prompt="go",
            config=SubagentConfig(name=f"worker-{index}", description="d", disallowed_tools=[]),
            holder=holder,
            model_name="deepseek-reasoner",
            label=f"job {index}",
            run_id="r1",
            thread_id="t1",
            user_id="u1",
            parent_model=None,
            uploaded_files=None,
            tools=[make_tool("read_file")],
            allowed_subagents=None,
            usage_recorder=None,
        )
        dispatches.append(dispatch)
        register(holder, run_id="r1")
        executor.start(dispatch)

    for _ in range(3):
        assert started.acquire(timeout=5), "三个子代理都该跑到模型调用里"

    asyncio.run(worker_module._finalize_subagents("r1"))

    assert registry.holders_for_run("r1") == [], "收尾之后注册表必须干净"
    assert capacity.snapshot().running == 0, "槽位一个都不许漏"
    assert [dispatch.holder.status for dispatch in dispatches] == [SubagentStatus.CANCELLED] * 3
