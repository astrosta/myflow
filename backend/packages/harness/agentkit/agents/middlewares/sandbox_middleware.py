"""Acquire on the way in, release on the way out. Tools never release the sandbox.

M8 §11（D7）：release 不再由"某一次执行结束"触发，而是由**最后一个租约持有者**触发。
lead 是 owner（发起 park），子代理是 non-owner 持有者（挡住父清理，自己不发起 park）。
一个子代理跑完就把沙箱关掉，兄弟就没得用了——这正是这一层要挡的。

本文件住在 ``agents/middlewares/``：它和别的中间件是同一类东西（图上的钩子），
租约语义仍然全在 ``agentkit.sandbox.lease`` 里，这里只是调用者。

**这里只管正常路径**：钩子是图里的节点，异常 / 协作式取消 / 超时展开三条路径不会走到
``after_agent``（§11.4）。那三条由 ``SubagentExecutor`` 的收尾调
:func:`agentkit.sandbox.lease.release_sandbox_lease`（先排空在途调用，再幂等释放）兜住。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from agentkit.sandbox.lease import (
    current_sandbox_lease,
    get_sandbox_lease_manager,
    lead_lease_owner_id,
    lease_owner_from_context,
)
from agentkit.sandbox.prewarm import (
    close_sandbox_prewarm,
    close_sandbox_prewarm_async,
    start_sandbox_prewarm,
)
from agentkit.sandbox.runtime import (
    reset_authorization_checked,
    runtime_user_bucket,
)
from agentkit.sandbox.sandbox_provider import get_sandbox_provider


class SandboxMiddleware(AgentMiddleware):
    """Acquire the thread sandbox around one agent execution and release it after.

    OpenSandbox can opt into a speculative background acquire; eager providers
    still acquire here. Either way a single tool must not release: siblings in
    the same step still need the sandbox. M8 又加了一条：**单个执行**也不能释放——
    兄弟子代理还在跑（§11）。``ensure_sandbox_initialized`` remains the lazy
    acquisition path used by tools.
    """

    def before_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        reset_authorization_checked()
        return _acquire(state, runtime, async_acquire=False)

    async def abefore_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        reset_authorization_checked()
        return await _acquire_async(state, runtime)

    def after_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        _release(state, runtime)
        return None

    async def aafter_agent(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        await _release_async(state, runtime)
        return None


def _thread_id(runtime: Runtime) -> str | None:
    context = getattr(runtime, "context", None) or {}
    if isinstance(context, dict) and context.get("thread_id"):
        return str(context["thread_id"])
    config = getattr(runtime, "config", None)
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _user_id(runtime: Runtime) -> str:
    """与工具路径同一个解析函数（M9 §2.1）：context.user_id → ContextVar → default 桶。"""
    return runtime_user_bucket(runtime)  # type: ignore[arg-type]


def _existing_id(state: Any) -> str | None:
    sandbox_state = state.get("sandbox") if isinstance(state, dict) else None
    if isinstance(sandbox_state, dict):
        return sandbox_state.get("sandbox_id")
    return None


def _lease_identity(runtime: Runtime) -> tuple[str | None, bool]:
    """``(owner_id, releasing)``：子代理的 context 里带 ``sandbox_lease_owner_id``。"""
    owner_id = lease_owner_from_context(getattr(runtime, "context", None))
    if owner_id is not None:
        return owner_id, False  # non-owner 持有者：挡住父清理，自己不发起 park
    return lead_lease_owner_id(_thread_id(runtime)), True


def _retain_lease(runtime: Runtime, sandbox_id: str) -> None:
    owner_id, releasing = _lease_identity(runtime)
    if owner_id is None:
        return
    get_sandbox_lease_manager().retain(
        owner_id, client=current_sandbox_lease(sandbox_id), releasing=releasing
    )


def _release_lease(runtime: Runtime, sandbox_id: str) -> None:
    """只摘掉**自己**那一次持有；真正释放由最后一个持有者完成（§11.1）。"""
    owner_id, _ = _lease_identity(runtime)
    if owner_id is None:
        get_sandbox_provider().release(sandbox_id)  # 没有身份信息：退回 M2 行为
        return
    get_sandbox_lease_manager().release(owner_id)


async def _release_lease_async(runtime: Runtime, sandbox_id: str) -> None:
    """``_release_lease`` 的 async 形状：远程 release 在线程池里跑，不占事件循环。"""
    owner_id, _ = _lease_identity(runtime)
    if owner_id is None:
        await get_sandbox_provider().release_async(sandbox_id)  # 退回 M2 行为
        return
    await get_sandbox_lease_manager().release_async(owner_id)


def _acquire(state: Any, runtime: Runtime, *, async_acquire: bool) -> dict[str, Any] | None:
    del async_acquire
    existing = _existing_id(state)
    if existing and get_sandbox_provider().get(existing) is not None:
        _stamp(runtime, existing)
        _retain_lease(runtime, existing)
        return None
    thread_id = _thread_id(runtime)
    if thread_id is None:
        return None
    provider = get_sandbox_provider()
    if getattr(provider, "prewarm_enabled", False):
        # Optional speculative acquire: submit it and return immediately so the
        # model can produce its first token. The first sandbox-backed tool waits
        # on the provider's per-thread lock and reuses the result.
        start_sandbox_prewarm(state, runtime, provider=provider)
        return None
    sandbox_id = provider.acquire(thread_id, user_id=_user_id(runtime))
    _stamp(runtime, sandbox_id)
    _retain_lease(runtime, sandbox_id)
    return {"sandbox": {"sandbox_id": sandbox_id}}


async def _acquire_async(state: Any, runtime: Runtime) -> dict[str, Any] | None:
    existing = _existing_id(state)
    if existing and get_sandbox_provider().get(existing) is not None:
        _stamp(runtime, existing)
        _retain_lease(runtime, existing)
        return None
    thread_id = _thread_id(runtime)
    if thread_id is None:
        return None
    provider = get_sandbox_provider()
    if getattr(provider, "prewarm_enabled", False):
        # Do not await: the worker overlaps sandbox creation with model time.
        start_sandbox_prewarm(state, runtime, provider=provider)
        return None
    sandbox_id = await provider.acquire_async(thread_id, user_id=_user_id(runtime))
    _stamp(runtime, sandbox_id)
    _retain_lease(runtime, sandbox_id)
    return {"sandbox": {"sandbox_id": sandbox_id}}


def _stamp(runtime: Runtime, sandbox_id: str) -> None:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        context["sandbox_id"] = sandbox_id


def _release(state: Any, runtime: Runtime) -> None:
    close_sandbox_prewarm(state, runtime)
    sandbox_id = _sandbox_id(state, runtime)
    if sandbox_id:
        _release_lease(runtime, sandbox_id)


async def _release_async(state: Any, runtime: Runtime) -> None:
    """``aafter_agent`` 只有一个 await 时机：等 provider 的异步 release 结束。"""
    await close_sandbox_prewarm_async(state, runtime)
    sandbox_id = _sandbox_id(state, runtime)
    if sandbox_id:
        await _release_lease_async(runtime, sandbox_id)


def _sandbox_id(state: Any, runtime: Runtime) -> str | None:
    context = getattr(runtime, "context", None) or {}
    sandbox_id = context.get("sandbox_id") if isinstance(context, dict) else None
    return sandbox_id or _existing_id(state)
