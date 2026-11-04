"""Task 9: async release handle and gateway startup wiring.

The provider gains two lifecycle hooks the process owner can call without knowing
the concrete provider:

* ``startup_check()`` — validate config/dependencies at gateway startup, before the
  first request. It must never create a sandbox.
* ``release_async(sandbox_id)`` — a release the event loop can await. The base class
  offloads the sync ``release`` to a worker thread, so a remote pause/kill never runs
  on the loop.

The lease handle and the middleware then carry that awaitable all the way to
``aafter_agent``, which is the only place with an event loop at release time.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from agentkit.agents.middlewares import sandbox_middleware
from agentkit.agents.middlewares.sandbox_middleware import SandboxMiddleware
from agentkit.sandbox import lease as lease_module
from agentkit.sandbox import runtime
from agentkit.sandbox.lease import (
    SandboxLeaseClient,
    SandboxLeaseManager,
    reset_sandbox_lease_manager,
)
from agentkit.sandbox.sandbox_provider import SandboxProvider


class FakeProvider:
    """Records which path a caller took: sync release or async release."""

    def __init__(self) -> None:
        self.checked = 0
        self.released: list[str] = []
        self.released_async: list[str] = []
        self.sandboxes: dict[str, object] = {}

    def startup_check(self) -> None:
        self.checked += 1

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        sandbox_id = f"cache:{thread_id}"
        self.sandboxes.setdefault(sandbox_id, object())
        return sandbox_id

    async def acquire_async(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        return self.acquire(thread_id, user_id=user_id)

    def get(self, sandbox_id: str) -> object | None:
        return self.sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        self.released.append(sandbox_id)

    async def release_async(self, sandbox_id: str) -> None:
        self.released_async.append(sandbox_id)


@pytest.fixture(autouse=True)
def _clean_lease_manager():
    reset_sandbox_lease_manager()
    yield
    reset_sandbox_lease_manager()


# ---------------------------------------------------------------- startup hook


def test_start_sandbox_provider_calls_startup_check(monkeypatch) -> None:
    """Gateway startup must run the provider's checks once, and return the provider."""
    fake = FakeProvider()
    monkeypatch.setattr(runtime, "get_sandbox_provider", lambda: fake)

    assert runtime.start_sandbox_provider() is fake
    assert fake.checked == 1


def test_start_sandbox_provider_forwards_the_app_config(monkeypatch) -> None:
    """The provider is built from the config the gateway booted with, not a re-read."""
    fake = FakeProvider()
    seen: list[dict[str, object]] = []

    def _get(**kwargs: object) -> FakeProvider:
        seen.append(kwargs)
        return fake

    monkeypatch.setattr(runtime, "get_sandbox_provider", _get)

    assert runtime.start_sandbox_provider(app_config="cfg") is fake
    assert seen == [{"app_config": "cfg"}]
    assert fake.checked == 1


def test_start_sandbox_provider_propagates_a_failed_check(monkeypatch) -> None:
    """A failed startup check aborts startup: no half-initialized provider is kept."""
    class Exploding(FakeProvider):
        def startup_check(self) -> None:
            raise RuntimeError("cube unreachable")

    monkeypatch.setattr(runtime, "get_sandbox_provider", lambda: Exploding())

    with pytest.raises(RuntimeError, match="cube unreachable"):
        runtime.start_sandbox_provider()


# ------------------------------------------------------- base release_async


def test_provider_release_async_default_offloads() -> None:
    """The base default must not run ``release`` on the event loop."""
    calls: list[str] = []
    threads: list[int] = []

    class Shim(SandboxProvider):
        def acquire(self, thread_id=None, *, user_id=None):
            return "s1"

        def get(self, sandbox_id):
            return None

        def release(self, sandbox_id):
            calls.append(sandbox_id)
            threads.append(threading.get_ident())

    asyncio.run(Shim().release_async("s1"))

    assert calls == ["s1"]
    assert threads != [threading.get_ident()], "release must run in a worker thread"


def test_base_startup_check_is_a_no_op() -> None:
    class Shim(SandboxProvider):
        def acquire(self, thread_id=None, *, user_id=None):
            return "s1"

        def get(self, sandbox_id):
            return None

        def release(self, sandbox_id):
            return None

    assert Shim().startup_check() is None


# ------------------------------------------------------------ lease handle


def test_lease_client_release_now_async_prefers_the_async_release() -> None:
    calls: list[str] = []
    client = SandboxLeaseClient(
        sandbox_id="cache:t1",
        release=lambda sandbox_id: calls.append(f"sync:{sandbox_id}"),
        release_async=_recorder(calls, "async"),
    )

    asyncio.run(client.release_now_async())

    assert calls == ["async:cache:t1"]


def test_lease_client_release_now_async_falls_back_to_the_sync_release() -> None:
    calls: list[str] = []
    client = SandboxLeaseClient(
        sandbox_id="cache:t1", release=lambda sandbox_id: calls.append(f"sync:{sandbox_id}")
    )

    asyncio.run(client.release_now_async())

    assert calls == ["sync:cache:t1"]


def test_current_sandbox_lease_picks_up_the_providers_async_release(monkeypatch) -> None:
    fake = FakeProvider()
    monkeypatch.setattr(lease_module, "get_sandbox_provider", lambda **kwargs: fake)

    client = lease_module.current_sandbox_lease("cache:t1")
    asyncio.run(client.release_now_async())

    assert fake.released_async == ["cache:t1"]
    assert fake.released == []


def test_lease_manager_release_async_awaits_the_async_release(monkeypatch) -> None:
    """The last holder leaving is the moment the sandbox dies: use the async path."""
    fake = FakeProvider()
    monkeypatch.setattr(lease_module, "get_sandbox_provider", lambda **kwargs: fake)
    manager = SandboxLeaseManager()
    manager.retain("lead:t1", client=lease_module.current_sandbox_lease("cache:t1"))

    assert asyncio.run(manager.release_async("lead:t1")) is True

    assert fake.released_async == ["cache:t1"]
    assert fake.released == []


def test_lease_manager_release_async_keeps_the_sync_path_for_other_holders() -> None:
    fake = FakeProvider()
    manager = SandboxLeaseManager()
    client = SandboxLeaseClient(
        sandbox_id="cache:t1", release=fake.release, release_async=fake.release_async
    )
    manager.retain("lead:t1", client=client)
    manager.retain("subagent:e1", client=client, releasing=False)

    assert asyncio.run(manager.release_async("lead:t1")) is False
    assert fake.released_async == []

    assert asyncio.run(manager.release_async("subagent:e1")) is True
    assert fake.released_async == ["cache:t1"]


def test_lease_manager_release_async_is_idempotent() -> None:
    fake = FakeProvider()
    manager = SandboxLeaseManager()
    client = SandboxLeaseClient(
        sandbox_id="cache:t1", release=fake.release, release_async=fake.release_async
    )
    manager.retain("lead:t1", client=client)

    assert asyncio.run(manager.release_async("lead:t1")) is True
    assert asyncio.run(manager.release_async("lead:t1")) is False

    assert fake.released_async == ["cache:t1"]


# -------------------------------------------------------------- middleware


class _Runtime:
    """The two attributes ``SandboxMiddleware`` reads off a graph runtime."""

    def __init__(self, context: dict) -> None:
        self.context = context
        self.config = {"configurable": {"thread_id": context.get("thread_id", "")}}


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(sandbox_middleware, "get_sandbox_provider", lambda **kwargs: fake)
    monkeypatch.setattr(lease_module, "get_sandbox_provider", lambda **kwargs: fake)
    return fake


def test_aafter_agent_awaits_the_async_release(provider: FakeProvider) -> None:
    """``aafter_agent`` is the one hook with an event loop: it must await the release."""
    middleware = SandboxMiddleware()
    graph_runtime = _Runtime({"thread_id": "t1", "user_id": "default"})

    async def scenario() -> None:
        await middleware.abefore_agent({}, graph_runtime)
        await middleware.aafter_agent({}, graph_runtime)

    asyncio.run(scenario())

    assert provider.released_async == ["cache:t1"]
    assert provider.released == [], "the loop must not block on the sync release"


def test_after_agent_still_releases_synchronously(provider: FakeProvider) -> None:
    """Sync graphs have no loop to await on: the M2 sync path is unchanged."""
    middleware = SandboxMiddleware()
    graph_runtime = _Runtime({"thread_id": "t1", "user_id": "default"})

    middleware.before_agent({}, graph_runtime)
    middleware.after_agent({}, graph_runtime)

    assert provider.released == ["cache:t1"]
    assert provider.released_async == []


def _recorder(calls: list[str], prefix: str):
    async def record(sandbox_id: str) -> None:
        calls.append(f"{prefix}:{sandbox_id}")

    return record
