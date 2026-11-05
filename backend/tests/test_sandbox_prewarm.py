"""Speculative sandbox prewarm must never block the first model token."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares import sandbox_middleware
from agentkit.sandbox.prewarm import (
    PREWARM_CONTEXT_KEY,
    SandboxPrewarm,
    should_prewarm_sandbox,
)


class SlowProvider:
    """Acquire sleeps; a blocking caller is visible in the test's wall clock."""

    prewarm_enabled = True
    prewarm_max_concurrent = 1

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, thread_id=None, *, user_id=None) -> str:
        self.acquired.append(thread_id or "")
        time.sleep(self.delay)
        return f"slow:{thread_id}"

    async def acquire_async(self, thread_id=None, *, user_id=None) -> str:
        return await asyncio.to_thread(self.acquire, thread_id, user_id=user_id)

    def get(self, sandbox_id: str) -> object | None:
        return object()

    def release(self, sandbox_id: str) -> None:
        self.released.append(sandbox_id)

    async def release_async(self, sandbox_id: str) -> None:
        self.released.append(sandbox_id)


def test_prewarm_signals_only_when_the_user_turn_looks_tool_shaped() -> None:
    assert should_prewarm_sandbox({"messages": [HumanMessage("你好，介绍一下你自己")]}) is False
    assert should_prewarm_sandbox({"messages": [HumanMessage("看看 https://example.com/a")]}) is False
    assert should_prewarm_sandbox({"messages": [HumanMessage("读一下 /tmp/a.py")]}) is True
    assert should_prewarm_sandbox({"messages": [HumanMessage("```python\nprint(1)\n```")]}) is True
    assert should_prewarm_sandbox({"messages": [HumanMessage("运行 pytest tests/")]}) is True
    assert should_prewarm_sandbox({"messages": [HumanMessage("Traceback (most recent call last):")]}) is True


def test_prewarm_start_returns_before_acquire_finishes() -> None:
    provider = SlowProvider(delay=0.2)
    handle = SandboxPrewarm(provider, thread_id="t1", user_id="u1", owner_id="lead:t1")

    started = time.monotonic()
    handle.start()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert handle.result(timeout=2) == "slow:t1"
    handle.close_sync(claimed=False)
    assert provider.released == ["slow:t1"]


def test_prewarm_does_not_release_a_sandbox_claimed_by_a_tool() -> None:
    provider = SlowProvider(delay=0.0)
    handle = SandboxPrewarm(provider, thread_id="t1", user_id="u1", owner_id="lead:t1")
    handle.start()
    assert handle.result(timeout=2) == "slow:t1"

    handle.close_sync(claimed=True)

    assert provider.released == []


def test_prewarm_releases_the_concurrency_slot_when_acquire_finishes() -> None:
    from agentkit.sandbox import prewarm as prewarm_module

    provider = SlowProvider(delay=0.1)
    assert prewarm_module._take_slot(provider, 1) is True
    assert prewarm_module._take_slot(provider, 1) is False

    handle = SandboxPrewarm(provider, thread_id="t1", user_id="u1", owner_id="lead:t1")
    handle.start()
    assert handle.result(timeout=2) == "slow:t1"

    # The slot bounds concurrent startups, so a cancelled run that never
    # reaches after_agent cannot leave prewarm disabled forever.
    assert prewarm_module._take_slot(provider, 1) is True
    prewarm_module._return_slot(provider)


def test_async_middleware_defers_to_prewarm_without_waiting(monkeypatch) -> None:
    provider = SlowProvider(delay=0.2)
    monkeypatch.setattr(sandbox_middleware, "get_sandbox_provider", lambda **kwargs: provider)
    runtime = SimpleNamespace(
        context={"thread_id": "t1", "user_id": "u1"},
        config={"configurable": {"thread_id": "t1"}},
    )
    state = {"messages": [HumanMessage("读一下 /tmp/a.py")]}
    middleware = sandbox_middleware.SandboxMiddleware()

    async def scenario() -> float:
        started = time.monotonic()
        await middleware.abefore_agent(state, runtime)
        elapsed = time.monotonic() - started
        runtime.context[PREWARM_CONTEXT_KEY].result(timeout=2)
        await middleware.aafter_agent(state, runtime)
        return elapsed

    assert asyncio.run(scenario()) < 0.1
    assert provider.released == ["slow:t1"]


def test_stale_checkpoint_sandbox_does_not_suppress_prewarm(monkeypatch) -> None:
    class StaleProvider(SlowProvider):
        def get(self, sandbox_id: str) -> object | None:
            return None

    provider = StaleProvider(delay=0.0)
    monkeypatch.setattr(sandbox_middleware, "get_sandbox_provider", lambda **kwargs: provider)
    runtime = SimpleNamespace(
        context={"thread_id": "t1", "user_id": "u1"},
        config={"configurable": {"thread_id": "t1"}},
    )
    state = {
        "messages": [HumanMessage("读一下 /tmp/a.py")],
        "sandbox": {"sandbox_id": "from-a-dead-container"},
    }
    middleware = sandbox_middleware.SandboxMiddleware()

    middleware.before_agent(state, runtime)

    assert PREWARM_CONTEXT_KEY in runtime.context
    runtime.context[PREWARM_CONTEXT_KEY].result(timeout=2)
    middleware.after_agent(state, runtime)
    assert provider.released == ["slow:t1"]


def test_middleware_defers_to_prewarm_without_waiting(monkeypatch) -> None:
    provider = SlowProvider(delay=0.2)
    monkeypatch.setattr(sandbox_middleware, "get_sandbox_provider", lambda **kwargs: provider)
    runtime = SimpleNamespace(
        context={"thread_id": "t1", "user_id": "u1"},
        config={"configurable": {"thread_id": "t1"}},
    )
    state = {"messages": [HumanMessage("读一下 /tmp/a.py")]}
    middleware = sandbox_middleware.SandboxMiddleware()

    started = time.monotonic()
    middleware.before_agent(state, runtime)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert state.get("sandbox") is None
    assert PREWARM_CONTEXT_KEY in runtime.context

    # Let the background thread finish, then run the normal teardown path.
    runtime.context[PREWARM_CONTEXT_KEY].result(timeout=2)
    middleware.after_agent(state, runtime)
    assert provider.released == ["slow:t1"]


def test_a_subagent_does_not_start_a_speculative_acquire(monkeypatch) -> None:
    provider = SlowProvider(delay=0.0)
    monkeypatch.setattr("agentkit.sandbox.prewarm.get_sandbox_provider", lambda: provider)
    runtime = SimpleNamespace(
        context={
            "thread_id": "t1",
            "user_id": "u1",
            "sandbox_lease_owner_id": "subagent:exec-1",
        },
        config={"configurable": {"thread_id": "t1"}},
    )
    state = {"messages": [HumanMessage("读一下 /tmp/a.py")]}

    from agentkit.sandbox.prewarm import start_sandbox_prewarm

    assert start_sandbox_prewarm(state, runtime, provider=provider) is None
    assert provider.acquired == []


def test_lazy_tool_acquisition_registers_the_lease(monkeypatch) -> None:
    from langchain.tools import ToolRuntime

    from agentkit.sandbox import lease as lease_module
    from agentkit.sandbox import runtime as runtime_module
    from agentkit.sandbox.lease import get_sandbox_lease_manager, reset_sandbox_lease_manager

    provider = SlowProvider(delay=0.0)
    reset_sandbox_lease_manager()
    monkeypatch.setattr(runtime_module, "get_sandbox_provider", lambda **kwargs: provider)
    monkeypatch.setattr(lease_module, "get_sandbox_provider", lambda **kwargs: provider)
    tool_runtime = ToolRuntime(
        state={},
        context={"thread_id": "t1", "user_id": "u1"},
        config={"configurable": {"thread_id": "t1"}},
        stream_writer=lambda payload: None,
        tool_call_id="call-1",
        store=None,
    )

    sandbox = runtime_module.ensure_sandbox_initialized(tool_runtime)

    assert sandbox is not None
    assert get_sandbox_lease_manager().holders("slow:t1") == ["lead:t1"]
    assert tool_runtime.state["sandbox"] == {"sandbox_id": "slow:t1"}
    get_sandbox_lease_manager().release("lead:t1")
    assert provider.released == ["slow:t1"]
