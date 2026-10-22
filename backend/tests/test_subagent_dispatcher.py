"""M8 §9（接缝）/ §7.1 / §10.1：`SubagentDispatcher` 这个端口的契约。

`TaskTool` 里的 `SubagentDispatcher` 只有声明（`...` 就是 `Protocol` 的方法体），实现分散在
三处：§7.1 的执行器、§10.1 的 runtime、以及装配时的 `assemble_agent`。三件事必须成立：

1. 两处实现**结构上**满足端口（`@runtime_checkable` 之后可以真的 `isinstance`）；
2. `start` 必须**同步**：工具提交完立刻轮询，写成 `async def` 会让轮询和 `task_running`
   一起消失（§9.1 第 8 步）；
3. 工具自己绝不 await 提交结果——实现变形时必须**当场失败**，而不是留一张永远转圈的卡片。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from agentkit.config import AppConfig
from agentkit.subagents.executor import SubagentExecutor
from agentkit.subagents.runtime import SubagentRuntime
from agentkit.tools.builtin.task_tool import SubagentDispatcher, make_task_tool
from fakes.task_tool_harness import FakeDispatcher, FakeRuntime, make_app_config, make_tool


def _app_config() -> AppConfig:
    return AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )


def _holder():
    from agentkit.subagents.results import SubagentResult, SubagentStatus

    return SubagentResult(task_id="exec-1", trace_id="t", status=SubagentStatus.PENDING)


def test_both_implementations_satisfy_the_port() -> None:
    """端口是结构类型：§7 的执行器与 §10.1 的 runtime 都得能被当成执行侧用。"""
    app_config = _app_config()

    assert isinstance(SubagentExecutor(app_config=app_config), SubagentDispatcher)
    assert isinstance(SubagentRuntime.from_app_config(app_config), SubagentDispatcher)


def test_the_runtime_start_delegates_to_the_shared_executor() -> None:
    """§10.1：runtime 自己不跑图，它把提交转交给那个**进程级共享**的执行器。"""
    runtime = SubagentRuntime.from_app_config(_app_config())
    seen: list[object] = []
    runtime.executor = SimpleNamespace(start=seen.append)
    dispatch = SimpleNamespace(holder=_holder())

    assert runtime.start(dispatch) is None

    assert seen == [dispatch]


def test_both_starts_are_synchronous() -> None:
    """§9.1 第 8 步：写成 ``async def`` 就没有轮询、也没有 ``task_running`` 了。"""
    for implementation in (SubagentExecutor.start, SubagentRuntime.start):
        assert not inspect.iscoroutinefunction(implementation), implementation
        assert list(inspect.signature(implementation).parameters) == ["self", "dispatch"]


def test_the_tool_submits_once_before_it_polls() -> None:
    """提交必须发生在第一条事件之前，而且每次调用只提交一次。"""
    order: list[str] = []

    class _OrderedDispatcher(FakeDispatcher):
        def start(self, dispatch) -> None:
            order.append("start")
            super().start(dispatch)

    class _OrderedRuntime(FakeRuntime):
        def stream_writer(self, payload) -> None:
            order.append(str(payload.get("type")))
            super().stream_writer(payload)

    runtime = _OrderedRuntime(context={"run_id": "r1", "thread_id": "t1"})
    runtime.tools = [make_tool("read_file")]
    tool = make_task_tool(
        dispatcher=_OrderedDispatcher(),
        app_config=make_app_config(allow_host_bash=True),
        poll_interval=0.01,
    )

    asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="go", subagent_type="general-purpose", tool_call_id="call_1"
        )
    )

    assert order[0] == "start", "先提交，再发 task_started"
    assert order.count("start") == 1, "一次委派只提交一次"
    assert "task_started" in order and order[-1] == "task_completed"


def test_an_async_start_fails_the_delegation_instead_of_hanging() -> None:
    """端口被写坏时（``async def start``）当场失败：不许留一张永远转圈的卡片。"""

    class _CoroutineDispatcher:
        """故意违反契约：返回没人 await 的协程，并在被 await 时打标记。"""

        def __init__(self) -> None:
            self.awaited = False
            self.holder = None

        def start(self, dispatch):
            self.holder = dispatch.holder
            return self._later()

        async def _later(self) -> None:
            self.awaited = True

    dispatcher = _CoroutineDispatcher()
    runtime = FakeRuntime(context={"run_id": "r1", "thread_id": "t1"})
    runtime.tools = [make_tool("read_file")]
    tool = make_task_tool(
        dispatcher=dispatcher,
        app_config=make_app_config(allow_host_bash=True),
        poll_interval=0.01,
    )

    command = asyncio.run(
        tool.coroutine(
            runtime=runtime, prompt="go", subagent_type="general-purpose", tool_call_id="call_1"
        )
    )

    message = command.update["messages"][0]
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "must be synchronous" in message.content
    assert dispatcher.awaited is False, "工具绝不 await 提交结果"
    assert dispatcher.holder.status.value == "failed"
    assert runtime.event_types() == [], "失败发生在 task_started 之前，不该有半张卡片"