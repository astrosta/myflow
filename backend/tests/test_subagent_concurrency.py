"""M8 §4.2 / §10：并发上限、总量上限、以及"槽位耗尽时剥调用而不是报错"。

两个上限是**两件事**：

* ``subagent_runtime.max_running``：**同时**在跑几个（进程级，启动冻结，§10.2 的 FIFO 准入）
* ``subagents.max_total_per_run``：一次 lead run **累计**派了几个（可热重载，§10.3 的剥调用）

只有并发上限是不够的：一个长 run 可以在每个规划检查点都发起一批合法大小的委派，跑一整天。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agentkit.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware
from agentkit.config import (
    SUBAGENT_CONCURRENCY_CEILING,
    SubagentRuntimeConfig,
    effective_subagent_concurrency,
)
from agentkit.subagents import results
from agentkit.subagents.capacity import (
    REASON_QUEUE_FULL,
    REASON_QUEUE_TIMEOUT,
    REASON_REJECTED,
    AdmissionDenied,
    SubagentExecutionCapacity,
)
from agentkit.subagents.results import SubagentStatus
from agentkit.subagents.runtime import (
    SubagentRuntime,
    get_subagent_runtime,
    reset_subagent_runtime,
)
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import FakeChatModel
from fakes.task_tool_harness import FakeRuntime, make_app_config, make_tool


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    reset_subagent_runtime()
    yield
    results.reset_registry()
    reset_subagent_runtime()


# ---------------------------------------------------------------- §4.2 单一解析点


@pytest.mark.parametrize(
    ("requested", "max_running", "expected"),
    [
        (None, 3, 3),  # 默认：用满冻结容量
        (2, 3, 2),  # 请求更小 → 听请求
        (8, 3, 3),  # 请求更大 → 听控制器
        (None, 64, 64),
        (100, 3, 3),
        (100, 64, 64),  # 硬上限 64
        (5, 200, 5),  # 控制器数值超界时上限被夹到 64
        (0, 3, 1),  # 非法请求值：夹到 1，不抛异常
        (-5, 3, 1),
        (None, 0, 1),  # 坏配置也返回合法值
    ],
)
def test_effective_subagent_concurrency(requested, max_running, expected) -> None:
    assert effective_subagent_concurrency(requested, max_running=max_running) == expected


def test_ceiling_is_the_schema_ceiling() -> None:
    assert SUBAGENT_CONCURRENCY_CEILING == 64
    with pytest.raises(Exception):
        SubagentRuntimeConfig(max_running=SUBAGENT_CONCURRENCY_CEILING + 1)


def test_runtime_config_reads_the_same_number() -> None:
    cfg = SubagentRuntimeConfig(max_running=3)
    assert cfg.effective_max_concurrent() == effective_subagent_concurrency(None, max_running=3)
    assert cfg.effective_max_concurrent(8) == effective_subagent_concurrency(8, max_running=3)


# ---------------------------------------------------------------- §10.2 异步 FIFO 准入


def _capacity(**overrides) -> SubagentExecutionCapacity:
    params = {"max_running": 2, "max_queued": 4, "queue_timeout_seconds": 5, **overrides}
    return SubagentExecutionCapacity(SubagentRuntimeConfig(**params))


def test_capacity_admits_up_to_max_running() -> None:
    capacity = _capacity(max_running=2)

    async def scenario() -> tuple[int, int]:
        async with capacity.slot():
            first = capacity.snapshot().running
            async with capacity.slot():
                return first, capacity.snapshot().running

    assert asyncio.run(scenario()) == (1, 2)
    assert capacity.snapshot().running == 0


def test_waiters_are_served_fifo_and_slots_are_handed_over() -> None:
    capacity = _capacity(max_running=1, max_queued=4)
    order: list[str] = []

    async def hold(name: str, release: asyncio.Event) -> None:
        async with capacity.slot():
            order.append(name)
            await release.wait()

    async def scenario() -> None:
        gate = asyncio.Event()  # 三个都用同一个闸门：放行之后它们会依次拿到槽位并立刻结束
        first = asyncio.create_task(hold("first", gate))
        while capacity.snapshot().running < 1:
            await asyncio.sleep(0)
        second = asyncio.create_task(hold("second", gate))
        third = asyncio.create_task(hold("third", gate))
        while capacity.snapshot().queued < 2:
            await asyncio.sleep(0)
        assert capacity.snapshot().queued == 2
        gate.set()  # 放掉第一个 → 槽位直接转交给队首
        await asyncio.wait_for(asyncio.gather(first, second, third), timeout=5)

    asyncio.run(scenario())
    assert order == ["first", "second", "third"]
    assert capacity.snapshot().running == 0


def test_a_full_queue_is_denied() -> None:
    capacity = _capacity(max_running=1, max_queued=1)

    async def scenario() -> str:
        async with capacity.slot():
            waiter = asyncio.create_task(_take(capacity))
            while capacity.snapshot().queued < 1:
                await asyncio.sleep(0)
            try:
                await _take(capacity)
            except AdmissionDenied as denied:
                return denied.reason
            finally:
                waiter.cancel()
        raise AssertionError("队列没满？")

    assert asyncio.run(scenario()) == REASON_QUEUE_FULL


async def _take(capacity: SubagentExecutionCapacity) -> None:
    async with capacity.slot():
        pass


def test_waiting_past_the_timeout_is_denied_and_leaves_the_queue() -> None:
    capacity = _capacity(max_running=1, max_queued=4, queue_timeout_seconds=0)

    async def scenario() -> str:
        async with capacity.slot():
            try:
                await _take(capacity)
            except AdmissionDenied as denied:
                assert capacity.snapshot().queued == 0, "超时后必须把自己从队列里摘掉"
                return denied.reason
        raise AssertionError("应该超时")

    assert asyncio.run(scenario()) == REASON_QUEUE_TIMEOUT


def test_the_reject_policy_denies_immediately() -> None:
    capacity = _capacity(max_running=1, admission_policy="reject")

    async def scenario() -> tuple[str, int]:
        async with capacity.slot():
            try:
                await _take(capacity)
            except AdmissionDenied as denied:
                return denied.reason, capacity.snapshot().queued
        raise AssertionError("应该被拒")

    assert asyncio.run(scenario()) == (REASON_REJECTED, 0)


def test_cancelling_a_waiting_task_releases_its_queue_position() -> None:
    capacity = _capacity(max_running=1, max_queued=4)

    async def scenario() -> None:
        gate = asyncio.Event()
        holder = asyncio.create_task(_hold_slot(capacity, gate))
        while capacity.snapshot().running < 1:
            await asyncio.sleep(0)
        waiter = asyncio.create_task(_take(capacity))
        while capacity.snapshot().queued < 1:
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert capacity.snapshot().queued == 0
        gate.set()
        await holder
        assert capacity.snapshot().running == 0, "取消不能把槽位一起吃掉"

    asyncio.run(scenario())


async def _hold_slot(capacity: SubagentExecutionCapacity, release: asyncio.Event) -> None:
    async with capacity.slot():
        await release.wait()


def test_snapshot_reports_the_frozen_configuration() -> None:
    capacity = _capacity(max_running=3, max_queued=7)
    snap = capacity.snapshot()
    assert (snap.max_running, snap.running, snap.queued, snap.policy) == (3, 0, 0, "queue")
    assert snap.available == 3

# ---------------------------------------------------------------- §10.1 共享控制器


def test_from_app_config_freezes_the_process_capacity() -> None:
    """§4.2：``subagent_runtime.*`` 是 startup-only——热重载改 YAML 不能抬高已建控制器。"""
    app_config = make_app_config()
    runtime = SubagentRuntime.from_app_config(app_config)

    app_config.subagent_runtime.max_running = 8
    assert runtime.config.max_running == 3
    assert runtime.effective_max_concurrent() == 3


def test_the_runtime_is_a_process_singleton() -> None:
    app_config = make_app_config()
    first = get_subagent_runtime(app_config)
    assert first is get_subagent_runtime(app_config)
    reset_subagent_runtime()
    assert get_subagent_runtime(app_config) is not first


def test_the_executor_shares_the_runtimes_capacity() -> None:
    runtime = SubagentRuntime.from_app_config(make_app_config())
    assert runtime.executor._capacity is runtime.execution_capacity


def _capture_assembly(monkeypatch, *, tools: list | None = None) -> dict:
    captured: dict = {}
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.get_available_tools",
        lambda *args, **kwargs: list(tools or []),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_agent",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(**kwargs),
    )
    return captured


def test_the_assembly_hands_the_same_runtime_to_tool_and_middleware(monkeypatch) -> None:
    """§10.1：task 闭包、Limit 中间件、执行器必须共用**同一个**实例。"""
    from agentkit.agents.leader_agent import assemble_agent

    captured = _capture_assembly(monkeypatch, tools=[make_tool("read_file")])
    app_config = make_app_config(allow_host_bash=True)
    runtime = SubagentRuntime.from_app_config(app_config)

    assemble_agent(
        {"configurable": {"subagent_enabled": True}},
        app_config=app_config,
        subagent_runtime=runtime,
    )

    assert [tool.name for tool in captured["tools"]] == ["read_file", "task"]
    limits = [m for m in captured["middleware"] if isinstance(m, SubagentLimitMiddleware)]
    assert len(limits) == 1
    assert limits[0].max_concurrent == runtime.effective_max_concurrent()
    assert limits[0].max_total_per_run == app_config.subagents.max_total_per_run


def test_the_real_lead_graph_compiles_with_the_limit_middleware(monkeypatch) -> None:
    """装配真的能编译：Limit 中间件成了图上的一个节点（不是只存在于参数里）。"""
    from agentkit.agents.leader_agent import assemble_agent

    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda **kwargs: FakeChatModel(reply="ok"),
    )
    app_config = make_app_config()
    runtime = SubagentRuntime.from_app_config(app_config)

    assembly = assemble_agent(
        {"configurable": {"subagent_enabled": True}},
        app_config=app_config,
        subagent_runtime=runtime,
    )

    nodes = set(assembly.graph.get_graph().nodes)
    assert "SubagentLimitMiddleware.after_model" in nodes
    assert "tools" in nodes


def test_the_assembly_leaves_subagents_out_when_the_client_disables_them(monkeypatch) -> None:
    from agentkit.agents.leader_agent import assemble_agent

    captured = _capture_assembly(monkeypatch)
    assemble_agent(
        {"configurable": {"subagent_enabled": False}},
        app_config=make_app_config(),
    )

    assert captured["tools"] == []
    assert not [m for m in captured["middleware"] if isinstance(m, SubagentLimitMiddleware)]


def test_the_assembly_installs_task_by_default(monkeypatch) -> None:
    """委派默认开启：检索工具已经移到子代理里，没有 task 就彻底失去检索能力。"""

    from agentkit.agents.leader_agent import assemble_agent

    captured = _capture_assembly(monkeypatch)
    assemble_agent({"configurable": {}}, app_config=make_app_config())

    assert [tool.name for tool in captured["tools"]] == ["task"]
    assert [m for m in captured["middleware"] if isinstance(m, SubagentLimitMiddleware)]


# ---------------------------------------------------------------- §10.3 剥调用


def _task_call(call_id: str) -> dict:
    return {
        "name": "task",
        "id": call_id,
        "args": {"prompt": "do it", "subagent_type": "general-purpose"},
    }


def _ai_message(*call_ids: str, content: str = "") -> AIMessage:
    return AIMessage(
        content=content,
        id="ai-1",
        tool_calls=[_task_call(call_id) for call_id in call_ids],
        additional_kwargs={
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "task", "arguments": "{}"}}
                for call_id in call_ids
            ]
        },
    )


def _delegation(entry_id: str, run_id: str) -> dict:
    return {
        "id": entry_id,
        "run_id": run_id,
        "description": "x",
        "subagent_type": "bash",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00Z",
    }


def _strip(state: dict, *, run_id: str = "run-1", max_concurrent: int = 3, max_total: int = 6):
    middleware = SubagentLimitMiddleware(max_concurrent=max_concurrent, max_total_per_run=max_total)
    context = {"run_id": run_id} if run_id is not None else {}
    return middleware.after_model(state, FakeRuntime(context=context))


def test_extra_task_calls_are_dropped_not_reported() -> None:
    update = _strip({"messages": [_ai_message("call-1", "call-2", "call-3")]}, max_concurrent=2)

    assert update is not None
    message = update["messages"][0]
    assert message.id == "ai-1", "同一个 id → add_messages 原地替换"
    assert [call["id"] for call in message.tool_calls] == ["call-1", "call-2"]
    assert "额度已用尽" not in message.content
    assert "本轮最多并行 2 个" in message.content
    assert "还剩 4 次" in message.content
    assert "do it" in message.content
    assert message.response_metadata.get("finish_reason") is None, "还有活要干，不能强制收尾"


def test_the_raw_provider_metadata_is_synced_with_the_dropped_call() -> None:
    """§10.3 第 5 步：只删结构化字段会留下悬空调用（严格后端 400）。"""
    update = _strip({"messages": [_ai_message("call-1", "call-2")]}, max_concurrent=1)

    raw = update["messages"][0].additional_kwargs["tool_calls"]
    assert [entry["id"] for entry in raw] == ["call-1"]


def test_dropping_everything_forces_a_stop_and_leaves_a_visible_notice() -> None:
    """``allowed == 0``：一个都不许派，模型必须用已有结果收尾。"""
    state = {
        "messages": [_ai_message("call-1")],
        "delegations": [_delegation(f"old-{index}", "run-1") for index in range(6)],
    }

    update = _strip(state, max_total=6)

    message = update["messages"][0]
    assert message.tool_calls == []
    assert message.additional_kwargs["tool_calls"] == []
    assert message.response_metadata["finish_reason"] == "stop"
    assert "额度已用尽" in message.content
    assert "以下任务未执行" in message.content


def test_history_runs_do_not_consume_this_runs_budget() -> None:
    state = {
        "messages": [_ai_message("call-1")],
        "delegations": [_delegation(f"old-{index}", "run-0") for index in range(6)],
    }
    assert _strip(state, max_total=6) is None


def test_a_missing_run_id_counts_the_whole_thread() -> None:
    """§10.2：偏严不偏松——预算数不清时宁可少派。"""
    state = {
        "messages": [_ai_message("call-1")],
        "delegations": [_delegation("old-1", "run-0")],
    }
    update = _strip(state, run_id=None, max_total=1)
    assert update is not None and update["messages"][0].tool_calls == []


def test_non_task_calls_are_untouched() -> None:
    """剥的只有 ``task``：同一轮里的普通工具调用照常执行。"""
    message = AIMessage(
        content="",
        id="ai-1",
        tool_calls=[_task_call("call-1"), {"name": "read_file", "id": "call-2", "args": {}}],
    )
    state = {"messages": [message], "delegations": [_delegation("old-1", "run-1")]}

    update = _strip(state, max_total=1)

    assert [call["name"] for call in update["messages"][0].tool_calls] == ["read_file"]


def test_no_task_calls_leaves_the_state_alone() -> None:
    assert _strip({"messages": [AIMessage(content="done", id="ai-1")]}) is None
    assert _strip({"messages": []}) is None


# ---------------------------------------------------------------- 端到端


_STATE: dict = {}
_RELEASE = threading.Event()


class _BlockingModel(FakeChatModel):
    """在模型里挂住，用来观察"同时有几个子代理真的在跑"。"""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        _STATE["running"] = _STATE.get("running", 0) + 1
        _STATE["peak"] = max(_STATE.get("peak", 0), _STATE["running"])
        try:
            await asyncio.get_running_loop().run_in_executor(None, _RELEASE.wait)
        finally:
            _STATE["running"] -= 1
        return self._result()


def _blocking_stack(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentkit.subagents.executor.create_chat_model",
        lambda **kwargs: _BlockingModel(reply="done"),
    )
    monkeypatch.setattr(
        "agentkit.subagents.executor.build_subagent_runtime_middlewares", lambda *a, **kw: []
    )


def _dispatch_once(tool, index: int):
    return tool.coroutine(
        runtime=FakeRuntime(context={"run_id": "run-1", "thread_id": "thread-1"}),
        prompt=f"job {index}",
        subagent_type="general-purpose",
        tool_call_id=f"call_{index}",
    )


def test_the_process_capacity_is_never_exceeded(monkeypatch) -> None:
    """§10.2：同时最多 ``max_running`` 个；多出来的在队列里等，**不是报错**。"""
    _blocking_stack(monkeypatch)
    _STATE.clear()
    _RELEASE.clear()
    app_config = make_app_config()
    runtime = SubagentRuntime(
        SubagentRuntimeConfig(max_running=1, max_queued=4), app_config=app_config
    )
    tool = make_task_tool(dispatcher=runtime, app_config=app_config, poll_interval=0.01)

    async def scenario() -> list:
        tasks = [asyncio.create_task(_dispatch_once(tool, index)) for index in range(3)]
        while _STATE.get("peak", 0) < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # 让另外两个走到排队那一步
        assert _STATE["running"] == 1
        assert runtime.snapshot().queued == 2
        _RELEASE.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)

    commands = asyncio.run(scenario())

    assert _STATE["peak"] == 1
    statuses = [c.update["messages"][0].additional_kwargs["subagent_status"] for c in commands]
    assert statuses == ["completed", "completed", "completed"]
    assert runtime.snapshot().running == 0


def test_an_admission_failure_is_a_normal_failed_delegation(monkeypatch) -> None:
    """§10.2：跑模型**之前**就被拒 → 普通失败的工具结果，没有 token 消耗。"""
    _blocking_stack(monkeypatch)
    app_config = make_app_config()
    runtime = SubagentRuntime(
        SubagentRuntimeConfig(max_running=1, admission_policy="reject"), app_config=app_config
    )

    async def scenario():
        async with runtime.execution_capacity.slot():  # 占满唯一的槽位
            tool = make_task_tool(dispatcher=runtime, app_config=app_config, poll_interval=0.01)
            return await tool.coroutine(
                runtime=FakeRuntime(context={"run_id": "run-1"}),
                prompt="job",
                subagent_type="general-purpose",
                tool_call_id="call_1",
            )

    command = asyncio.run(scenario())
    message = command.update["messages"][0]

    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "rejected" in message.additional_kwargs["subagent_error"]
    assert "不要立即重试" in message.additional_kwargs["subagent_error"]
    assert message.status == "error"
    assert command.update["delegations"][0]["status"] == "failed"
