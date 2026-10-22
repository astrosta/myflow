"""M8 §11：共享沙箱的引用租约（D7）。

M2 的模型是"中间件 acquire、中间件 release"。子代理和 lead **共用同一个 thread 沙箱**，
这个模型立刻破功：**一个子代理跑完了，不能把沙箱关掉——兄弟还在跑。**

所以 release 的时机是"**最后一个持有者**离开"，而不是"某一次执行结束"。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain.tools import ToolRuntime

from agentkit.agents.middlewares import sandbox_middleware
from agentkit.sandbox import lease as lease_module
from agentkit.sandbox.lease import (
    SandboxLeaseClient,
    drain_sandbox_work,
    get_sandbox_lease_manager,
    lead_lease_owner_id,
    reset_sandbox_lease_manager,
    shield_sandbox_work,
    subagent_lease_owner_id,
)
from agentkit.agents.middlewares.sandbox_middleware import SandboxMiddleware
from agentkit.subagents import executor as ex
from agentkit.subagents import results
from agentkit.tools.builtin.task_tool import make_task_tool
from fakes.chat_model import FakeChatModel
from fakes.task_tool_harness import FakeRuntime, make_app_config, make_tool

THREAD_ID = "thread-1"
SANDBOX_ID = f"fake:{THREAD_ID}"


@pytest.fixture(autouse=True)
def _clean_state():
    results.reset_registry()
    reset_sandbox_lease_manager()
    yield
    results.reset_registry()
    reset_sandbox_lease_manager()
    ex.shutdown_isolated_subagent_loop()


class FakeProvider:
    """只记账的 provider：``release`` 就等于"沙箱被关掉了"。"""

    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.sandboxes: dict[str, object] = {}

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        sandbox_id = f"fake:{thread_id}"
        if sandbox_id not in self.sandboxes:
            self.acquired.append(sandbox_id)
            self.sandboxes[sandbox_id] = object()
        return sandbox_id

    async def acquire_async(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        return self.acquire(thread_id, user_id=user_id)

    def get(self, sandbox_id: str) -> object | None:
        return self.sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        self.released.append(sandbox_id)
        self.sandboxes.pop(sandbox_id, None)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(sandbox_middleware, "get_sandbox_provider", lambda **kwargs: fake)
    monkeypatch.setattr(lease_module, "get_sandbox_provider", lambda **kwargs: fake)
    return fake


class _Runtime:
    """``AgentMiddleware`` 眼里的 runtime：context + config 两处。"""

    def __init__(self, context: dict) -> None:
        self.context = context
        self.config = {"configurable": {"thread_id": context.get("thread_id", "")}}


def _lead() -> tuple[SandboxMiddleware, _Runtime]:
    return SandboxMiddleware(), _Runtime({"thread_id": THREAD_ID, "user_id": "default"})


def _fake_ensure(factory):
    async def _ensure(runtime):
        return factory()

    return _ensure


def _tool_runtime(context: dict) -> ToolRuntime:
    """真 ``ToolRuntime``：bash 工具的参数会被 pydantic 校验，替身对象过不去。"""
    return ToolRuntime(
        state={},
        context=context,
        config={"configurable": {"thread_id": THREAD_ID}},
        stream_writer=lambda payload: None,
        tool_call_id="call-1",
        store=None,
    )


def _subagent(execution_id: str = "exec-1") -> tuple[SandboxMiddleware, _Runtime]:
    return SandboxMiddleware(), _Runtime({
        "thread_id": THREAD_ID,
        "user_id": "default",
        "sandbox_lease_owner_id": subagent_lease_owner_id(execution_id),
        "sandbox_command_scope_id": subagent_lease_owner_id(execution_id),
    })


# ---------------------------------------------------------------- §11.1 引用租约


def test_the_lead_acquires_and_owns_the_lease(provider: FakeProvider) -> None:
    middleware, runtime = _lead()

    middleware.before_agent({}, runtime)

    assert provider.acquired == [SANDBOX_ID]
    assert get_sandbox_lease_manager().holders(SANDBOX_ID) == [lead_lease_owner_id(THREAD_ID)]


def test_a_finished_subagent_does_not_close_the_shared_sandbox(provider: FakeProvider) -> None:
    """**这条就是 §11 存在的理由**：兄弟还在跑。"""
    lead, lead_runtime = _lead()
    subagent, subagent_runtime = _subagent()
    lead.before_agent({}, lead_runtime)
    subagent.before_agent({}, subagent_runtime)

    subagent.after_agent({}, subagent_runtime)

    assert provider.released == []
    assert provider.get(SANDBOX_ID) is not None
    assert get_sandbox_lease_manager().holders(SANDBOX_ID) == [lead_lease_owner_id(THREAD_ID)]


def test_the_last_holder_releases_the_sandbox(provider: FakeProvider) -> None:
    lead, lead_runtime = _lead()
    subagent, subagent_runtime = _subagent()
    lead.before_agent({}, lead_runtime)
    subagent.before_agent({}, subagent_runtime)
    subagent.after_agent({}, subagent_runtime)

    lead.after_agent({}, lead_runtime)

    assert provider.released == [SANDBOX_ID]
    assert provider.get(SANDBOX_ID) is None
    assert get_sandbox_lease_manager().holders(SANDBOX_ID) == []


def test_the_parent_release_waits_for_the_subagent(provider: FakeProvider) -> None:
    """父先收工、子代理还在跑：park 请求挂起，最后一个持有者离开时才真的关。"""
    lead, lead_runtime = _lead()
    subagent, subagent_runtime = _subagent()
    lead.before_agent({}, lead_runtime)
    subagent.before_agent({}, subagent_runtime)

    lead.after_agent({}, lead_runtime)
    assert provider.released == [], "子代理还拿着租约"
    assert get_sandbox_lease_manager().pending_park() == [SANDBOX_ID]

    subagent.after_agent({}, subagent_runtime)
    assert provider.released == [SANDBOX_ID]


def test_a_non_owner_release_never_parks_the_sandbox(provider: FakeProvider) -> None:
    """§11.1：子代理**不发起 park**——只有 owner 的释放才构成 park 请求。"""
    subagent, subagent_runtime = _subagent()
    subagent.before_agent({}, subagent_runtime)

    subagent.after_agent({}, subagent_runtime)

    assert provider.released == []
    assert get_sandbox_lease_manager().pending_park() == []


def test_release_is_idempotent_on_both_paths(provider: FakeProvider) -> None:
    """§11.4：中间件正常路径释放一次，执行器的 finally 再释放一次，只有一次真的关。"""
    lead, lead_runtime = _lead()
    lead.before_agent({}, lead_runtime)
    owner_id = lead_lease_owner_id(THREAD_ID)

    lead.after_agent({}, lead_runtime)
    asyncio.run(get_sandbox_lease_manager().release_async(owner_id))

    assert provider.released == [SANDBOX_ID]


def test_the_second_acquire_of_the_same_thread_reuses_the_sandbox(provider: FakeProvider) -> None:
    """子代理的 fresh state 里没有 sandbox，但 provider 按 (user, thread) 复用同一个。"""
    lead, lead_runtime = _lead()
    subagent, subagent_runtime = _subagent()

    lead.before_agent({}, lead_runtime)
    subagent.before_agent({}, subagent_runtime)

    assert provider.acquired == [SANDBOX_ID]
    assert subagent_runtime.context["sandbox_id"] == SANDBOX_ID


# ---------------------------------------------------------------- §11.2 命令作用域


def test_the_bash_tool_passes_the_subagent_command_scope(monkeypatch) -> None:
    """命令作用域 = 子代理自己的 shell 会话；lead 没有 scope，走同一条直通路。"""
    import importlib
    from agentkit.sandbox.tools.bash_tool import bash_tool

    bash_module = importlib.import_module("agentkit.sandbox.tools.bash_tool")
    seen: list[str | None] = []

    class _Sandbox:
        def execute_command_in_scope(self, command, env=None, timeout=None, *, scope=None):
            seen.append(scope)
            return "ok"

    monkeypatch.setattr(bash_module, "ensure_sandbox_initialized", lambda runtime: _Sandbox())
    sandbox_runtime = importlib.import_module("agentkit.sandbox.runtime")
    monkeypatch.setattr(
        sandbox_runtime, "ensure_sandbox_initialized_async", _fake_ensure(_Sandbox)
    )

    scope = subagent_lease_owner_id("exec-1")
    runtime = _tool_runtime({"thread_id": THREAD_ID, "sandbox_command_scope_id": scope})
    assert asyncio.run(bash_tool.coroutine(command="ls", runtime=runtime)) == "ok"
    assert seen == [scope]

    lead_runtime = _tool_runtime({"thread_id": THREAD_ID})
    asyncio.run(bash_tool.coroutine(command="ls", runtime=lead_runtime))
    assert seen == [scope, None]


# ---------------------------------------------------------------- §11.3 在途调用排空


def test_draining_waits_for_the_shielded_call() -> None:
    """``to_thread`` 取消不掉：取消之后还得等它跑完，才能释放租约。"""
    owner_id = subagent_lease_owner_id("exec-drain")
    finished: list[str] = []

    async def work() -> str:
        await asyncio.sleep(0.05)
        finished.append("done")
        return "done"

    async def scenario() -> int:
        task = asyncio.ensure_future(shield_sandbox_work(owner_id, work()))
        await asyncio.sleep(0)
        task.cancel()  # 调用方被砍：里面的活继续跑
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [], "还没跑完"
        drained = await drain_sandbox_work(owner_id)
        assert finished == ["done"]
        return drained

    assert asyncio.run(scenario()) == 1
    assert asyncio.run(drain_sandbox_work(owner_id)) == 0, "台账要清干净"


class _SlowLoopingModel(FakeChatModel):
    """每一轮都调工具，但**慢到轮次上限不会先到**（否则测的是 turn_capped 不是 timeout）。"""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        await asyncio.sleep(0.05)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="", tool_calls=[{"name": "noop", "id": "call-x", "args": {}}]
                    )
                )
            ]
        )


def _sandbox_subagent_stack(monkeypatch) -> None:
    """子代理链上挂**真的** ``SandboxMiddleware``（provider 是替身），其他都最简。"""
    monkeypatch.setattr(
        "agentkit.subagents.executor.build_subagent_runtime_middlewares",
        lambda *args, **kwargs: [SandboxMiddleware()],
    )
    monkeypatch.setattr(
        "agentkit.subagents.executor.create_chat_model",
        lambda **kwargs: _SlowLoopingModel(reply=""),
    )


def _sandbox_tool(app_config, *, runtime: FakeRuntime):
    tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config), app_config=app_config, poll_interval=0.01
    )
    runtime.tools = [make_tool("noop")]
    return tool


def _wait_for_lease() -> None:
    for _ in range(500):
        if get_sandbox_lease_manager().holders(SANDBOX_ID):
            return
    raise AssertionError("子代理一直没拿到沙箱租约")


def test_a_cancelled_subagent_leaves_no_lease(provider: FakeProvider, monkeypatch) -> None:
    """§17 + §11.4：取消可能绕过中间件的 ``after_agent``，执行器的 finally 必须兜住。"""
    _sandbox_subagent_stack(monkeypatch)
    app_config = make_app_config()
    runtime = FakeRuntime(context={"run_id": "run-1", "thread_id": THREAD_ID})
    tool = _sandbox_tool(app_config, runtime=runtime)

    async def scenario():
        task = asyncio.create_task(
            tool.coroutine(
                runtime=runtime,
                prompt="loop forever",
                subagent_type="general-purpose",
                tool_call_id="call_1",
            )
        )
        while not get_sandbox_lease_manager().holders(SANDBOX_ID):
            await asyncio.sleep(0.01)
        leased = get_sandbox_lease_manager().holders(SANDBOX_ID)
        results.cancel_all_for_run("run-1")
        return await asyncio.wait_for(task, timeout=15), leased

    command, leased = asyncio.run(scenario())

    assert leased, "子代理要先真的持有租约"
    assert command.update["messages"][0].additional_kwargs["subagent_status"] == "cancelled"
    assert get_sandbox_lease_manager().holders(SANDBOX_ID) == [], "取消不能泄漏租约"
    assert provider.released == [], "没有 owner 发起 park，沙箱不该被关掉"


def test_a_timed_out_subagent_leaves_no_lease(provider: FakeProvider, monkeypatch) -> None:
    """墙钟到点走的是 ``wait_for`` 的取消路径，同样要兜底释放租约。"""
    _sandbox_subagent_stack(monkeypatch)
    app_config = make_app_config(agents={"general-purpose": {"timeout_seconds": 1}})
    runtime = FakeRuntime(context={"run_id": "run-1", "thread_id": THREAD_ID})
    tool = _sandbox_tool(app_config, runtime=runtime)

    command = asyncio.run(
        tool.coroutine(
            runtime=runtime,
            prompt="loop forever",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )

    assert command.update["messages"][0].additional_kwargs["subagent_status"] == "timed_out"
    assert get_sandbox_lease_manager().holders(SANDBOX_ID) == []


def test_the_executor_releases_through_the_single_entry_point(monkeypatch) -> None:
    """§11.4：收尾必须走 ``release_sandbox_lease``——排空 + 幂等释放只有那一个入口。"""
    seen: list[str] = []
    monkeypatch.setattr(
        "agentkit.subagents.executor.build_subagent_runtime_middlewares", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "agentkit.subagents.executor.create_chat_model", lambda **kw: FakeChatModel(reply="done")
    )

    async def fake_release(owner_id):
        seen.append(owner_id)
        return True

    monkeypatch.setattr("agentkit.subagents.executor.release_sandbox_lease", fake_release)

    app_config = make_app_config()
    tool = make_task_tool(
        dispatcher=ex.SubagentExecutor(app_config=app_config), app_config=app_config, poll_interval=0.01
    )
    asyncio.run(
        tool.coroutine(
            runtime=FakeRuntime(context={"run_id": "run-1", "thread_id": THREAD_ID}),
            prompt="job",
            subagent_type="general-purpose",
            tool_call_id="call_1",
        )
    )

    assert len(seen) == 1
    assert seen[0].startswith("subagent:")  # owner id 派生自 execution_id（§11.1）


def test_release_sandbox_lease_drains_before_releasing(monkeypatch) -> None:
    """§11.3 的顺序：先等线程池里那笔调用跑完，再摘持有——反了就会打到已关闭的沙箱上。"""
    order: list[str] = []

    async def fake_drain(owner_id, **kwargs):
        order.append("drain")
        return 0

    class _Manager:
        async def release_async(self, owner_id):
            order.append("release")
            return True

    monkeypatch.setattr(lease_module, "drain_sandbox_work", fake_drain)
    monkeypatch.setattr(lease_module, "get_sandbox_lease_manager", lambda: _Manager())

    assert asyncio.run(lease_module.release_sandbox_lease("subagent:exec-1")) is True
    assert order == ["drain", "release"]


def test_release_sandbox_lease_is_idempotent(provider: FakeProvider) -> None:
    """中间件可能已经摘过持有：收尾再调一次不许重复 ``provider.release``（§11.4）。"""
    lead, lead_runtime = _lead()
    subagent, subagent_runtime = _subagent("exec-idem")
    lead.before_agent({}, lead_runtime)
    subagent.before_agent({}, subagent_runtime)
    lead.after_agent({}, lead_runtime)  # 只发起 park：子代理还持有
    assert provider.released == []

    owner = subagent_lease_owner_id("exec-idem")
    assert asyncio.run(lease_module.release_sandbox_lease(owner)) is True
    assert provider.released == [SANDBOX_ID], "最后一个持有者离开才真的关"

    assert asyncio.run(lease_module.release_sandbox_lease(owner)) is False
    assert provider.released == [SANDBOX_ID], "重复调用不许再关一次"
