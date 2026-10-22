"""沙箱引用租约（M8 §11，D7）。

M2 的模型是"中间件 acquire、中间件 release"。子代理和 lead **共用同一个 thread 沙箱**，
这个模型立刻破功：**一个子代理跑完了，不能把沙箱关掉——兄弟还在跑。**

于是 release 的时机从"某一次执行结束"升格为"**最后一个持有者**离开"：

* lead 是 **owner**（``releasing=True``）：它的释放是**发起 park**，真正的
  ``provider.release`` 要等所有持有者都走了。
* 子代理是 **non-owner 持有者**（``releasing=False``）：挡住父清理、拥有自己的
  command scope，但自己不发起 park。

M2 的"release 不能由单个工具触发"到这里升格为"release 不能由单个**执行**触发"。

另外两条与它配套的纪律：

* **兜底释放**（§11.4）：异常 / 协作式取消 / 超时展开这三条路径都可能绕过中间件的
  ``after_agent``，所以执行器在 ``finally`` 里再释放一次（幂等），否则租约泄漏。
* **在途调用要排空**（§11.3）：``to_thread`` 里的同步沙箱调用**取消不掉**。worker 被砍
  之后线程还在跑，不排空就释放租约，它会在终态化之后再去碰一个已经被释放的沙箱。
  ``shield_sandbox_work`` 负责登记，``drain_sandbox_work`` 负责排空。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentkit.sandbox.sandbox_provider import get_sandbox_provider

logger = logging.getLogger(__name__)

#: 租约 id 的前缀。子代理的 id 派生自 ``execution_id``，**不用** provider 的 tool_call_id（§2）。
SUBAGENT_LEASE_PREFIX = "subagent:"
LEAD_LEASE_PREFIX = "lead:"

#: 排空在途沙箱调用的等待上限（秒）。
DRAIN_TIMEOUT_SECONDS = 30.0

#: 子代理 context 里那个键（§11.1）。§7 的执行器把两个 id 放进子代理的 runtime.context。
LEASE_OWNER_CONTEXT_KEY = "sandbox_lease_owner_id"
COMMAND_SCOPE_CONTEXT_KEY = "sandbox_command_scope_id"


def subagent_lease_owner_id(execution_id: str) -> str:
    return f"{SUBAGENT_LEASE_PREFIX}{execution_id}"


def lead_lease_owner_id(thread_id: str | None) -> str | None:
    return f"{LEAD_LEASE_PREFIX}{thread_id}" if thread_id else None


def lease_owner_from_context(context: object) -> str | None:
    """子代理的 context 里带着租约 id；lead 没有（它自己是 owner）。"""
    if isinstance(context, Mapping):
        owner = context.get(LEASE_OWNER_CONTEXT_KEY)
        if isinstance(owner, str) and owner:
            return owner
    return None


def command_scope_from_context(context: object) -> str | None:
    """§11.2：每个子代理一个 command scope（独立 shell 会话）。"""
    if isinstance(context, Mapping):
        scope = context.get(COMMAND_SCOPE_CONTEXT_KEY)
        if isinstance(scope, str) and scope:
            return scope
    return None


@dataclass(frozen=True, slots=True)
class SandboxLeaseClient:
    """释放句柄：钉住 **retain 那一刻的 provider 客户端**。

    provider 单例被 reset / 热重载换掉之后，旧持有者绝不能去释放新单例里的沙箱——
    所以这里存的是那个 release 函数本身，不是"到时候再查一次单例"。
    """

    sandbox_id: str
    release: Callable[[str], None]

    def release_now(self) -> None:
        self.release(self.sandbox_id)


def current_sandbox_lease(sandbox_id: str) -> SandboxLeaseClient:
    """按**现在**的 provider 单例造一个句柄（retain 时调一次）。"""
    return SandboxLeaseClient(sandbox_id=sandbox_id, release=get_sandbox_provider().release)


@dataclass(slots=True)
class _Holder:
    client: SandboxLeaseClient
    releasing: bool


class SandboxLeaseManager:
    """按 owner id 登记持有关系；**最后一个持有者**执行真正的 release。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_owner: dict[str, _Holder] = {}
        self._by_sandbox: dict[str, set[str]] = {}
        self._park_requested: set[str] = set()

    # ---- 持有 -------------------------------------------------------------

    def retain(self, owner_id: str, *, client: SandboxLeaseClient, releasing: bool = True) -> None:
        """登记一次持有。同一个 owner 重复登记是幂等的（沿用第一次的 client）。"""
        with self._lock:
            if owner_id in self._by_owner:
                return
            self._by_owner[owner_id] = _Holder(client=client, releasing=releasing)
            self._by_sandbox.setdefault(client.sandbox_id, set()).add(owner_id)

    async def retain_async(
        self, owner_id: str, *, client: SandboxLeaseClient, releasing: bool = True
    ) -> None:
        """``retain`` 的 async 形状：将来接远程 provider 时，await 点加在这一版里。"""
        self.retain(owner_id, client=client, releasing=releasing)

    # ---- 释放（幂等）------------------------------------------------------

    def release(self, owner_id: str) -> bool:
        """摘掉这个持有者。返回"这次调用是否真的触发了 ``provider.release``"。

        幂等：没持有过、或已经释放过，都返回 False 且什么都不做。真正的 release 只在
        **它是最后一个持有者**且 owner 已经发起过 park 时发生。
        """
        holder: _Holder | None
        should_release = False
        with self._lock:
            holder = self._by_owner.pop(owner_id, None)
            if holder is None:
                return False
            sandbox_id = holder.client.sandbox_id
            owners = self._by_sandbox.get(sandbox_id)
            if owners is not None:
                owners.discard(owner_id)
            if holder.releasing:
                self._park_requested.add(sandbox_id)
            if not owners:
                self._by_sandbox.pop(sandbox_id, None)
                should_release = sandbox_id in self._park_requested
                if should_release:
                    self._park_requested.discard(sandbox_id)
        if should_release:
            _release_now(holder.client)
        return should_release

    async def release_async(self, owner_id: str) -> bool:
        return self.release(owner_id)

    # ---- 只读视图（测试 / 诊断）------------------------------------------

    def holders(self, sandbox_id: str) -> list[str]:
        with self._lock:
            return sorted(self._by_sandbox.get(sandbox_id, ()))

    def pending_park(self) -> list[str]:
        with self._lock:
            return sorted(self._park_requested)

    def snapshot(self) -> dict[str, list[str]]:
        with self._lock:
            return {sandbox_id: sorted(owners) for sandbox_id, owners in self._by_sandbox.items()}

    def reset(self) -> None:
        """测试与进程关停用：丢掉所有持有关系（**不**释放任何沙箱）。"""
        with self._lock:
            self._by_owner.clear()
            self._by_sandbox.clear()
            self._park_requested.clear()


def _release_now(client: SandboxLeaseClient) -> None:
    try:
        client.release_now()
    except Exception:
        logger.exception("sandbox release failed for %s", client.sandbox_id)


_manager: SandboxLeaseManager | None = None
_manager_lock = threading.Lock()


def get_sandbox_lease_manager() -> SandboxLeaseManager:
    """进程级单例：租约是进程内的引用计数，跨 run / 跨子代理共享。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = SandboxLeaseManager()
        return _manager


def reset_sandbox_lease_manager() -> None:
    """测试与进程关停用。"""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.reset()
        _manager = None


# ---------------------------------------------------------------- §11.3 在途调用

_inflight_lock = threading.Lock()
_inflight: dict[str, set[asyncio.Task]] = {}


async def shield_sandbox_work(owner_id: str, awaitable: Awaitable[Any]) -> Any:
    """把一次沙箱调用登记到 owner 名下再 await（§11.3）。

    ``asyncio.shield`` 让调用方被取消时里面的活照样跑完（``to_thread`` 本来也取消不掉），
    登记是为了让执行器在释放租约之前能 ``drain_sandbox_work`` 等它。
    """
    task = asyncio.ensure_future(asyncio.shield(awaitable))
    _track(owner_id, task)
    return await asyncio.shield(task)


def _track(owner_id: str, task: asyncio.Task) -> None:
    with _inflight_lock:
        _inflight.setdefault(owner_id, set()).add(task)
    task.add_done_callback(lambda finished: _untrack(owner_id, finished))


def _untrack(owner_id: str, task: asyncio.Task) -> None:
    with _inflight_lock:
        tasks = _inflight.get(owner_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            _inflight.pop(owner_id, None)


async def drain_sandbox_work(owner_id: str, *, timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
    """等这个 owner 名下还在跑的沙箱调用结束（§11.3），返回排空了几条。

    必须在**拥有这些 task 的那个循环**上 await（执行器的 finally 就跑在隔离循环上）。
    """
    with _inflight_lock:
        tasks = [task for task in _inflight.get(owner_id, ()) if not task.done()]
    if not tasks:
        return 0
    logger.info(
        "draining %d in-flight sandbox call(s) for %s before releasing the lease",
        len(tasks),
        owner_id,
    )
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning(
            "%d sandbox call(s) for %s did not finish within %ss", len(pending), owner_id, timeout
        )
    return len(done)


async def release_sandbox_lease(owner_id: str) -> bool:
    """§11.3/§11.4 的**唯一释放入口**：先排空在途沙箱调用，再摘掉这次持有。

    顺序不能反：``release`` 有可能是最后一个持有者那一刻，provider 就在那里被关掉，
    而在途调用（``to_thread`` 里跑的 bash，见 :func:`shield_sandbox_work`）随后打过去
    就是打在一个已释放的沙箱上。

    幂等：中间件已经摘过自己的持有时，这里什么都不做，**不会重复** ``provider.release``。
    中间件的正常路径（``after_agent``）只摘持有、不排空——它拿不到这个 await 时机；
    异常 / 协作式取消 / 超时展开这三条绕过中间件的路径由执行器的收尾调这里（§11.4）。
    """
    await drain_sandbox_work(owner_id)
    return await get_sandbox_lease_manager().release_async(owner_id)


__all__ = [
    "COMMAND_SCOPE_CONTEXT_KEY",
    "DRAIN_TIMEOUT_SECONDS",
    "LEASE_OWNER_CONTEXT_KEY",
    "SandboxLeaseClient",
    "SandboxLeaseManager",
    "command_scope_from_context",
    "current_sandbox_lease",
    "drain_sandbox_work",
    "get_sandbox_lease_manager",
    "lead_lease_owner_id",
    "lease_owner_from_context",
    "release_sandbox_lease",
    "reset_sandbox_lease_manager",
    "shield_sandbox_work",
    "subagent_lease_owner_id",
]
