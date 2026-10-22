"""子代理的异步 FIFO 准入（M8 §10.2）。

进程级容量控制的全部实现：``max_running`` 个槽位 + 一个 FIFO 等待队列。它管的是
"**同时**在跑几个"，和 ``subagents.max_total_per_run``（一次 run 累计派几个）是两件事，
不要合成一个（§4.2）。

两条硬要求：

* **等待者不占调度线程**：等待是纯 ``asyncio.Future``，不是 ``threading.Semaphore``——
  后者会把隔离循环堵死，一个子代理排队就让所有子代理停摆。
* **取消和超时必须释放队列位置和槽位所有权**：漏一次就永久少一个槽位，而且漏得无声无息
  （``snapshot()`` 只会显示"忙"，看不出是漏的）。

准入失败（队列满 / 排队超时 / reject 策略）发生在**跑模型之前**：执行器把它映射成
``try_set_terminal(FAILED, error=..., admission_failure=True)``，``task`` 工具再把它变成
一条普通失败 ToolMessage——**不消耗"成功委派"语义**，但总量台账仍记一条 ``failed``，
免得模型原地重试把队列打满。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from agentkit.config import SubagentRuntimeConfig

logger = logging.getLogger(__name__)

#: 准入失败的原因码。执行器把它写进 ``SubagentResult.error``，测试与运维都按它分桶。
REASON_QUEUE_FULL = "queue_full"
REASON_REJECTED = "rejected"
REASON_QUEUE_TIMEOUT = "queue_timeout"


class AdmissionDenied(RuntimeError):
    """没有拿到槽位。``reason`` 是上面三个原因码之一。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    max_running: int
    running: int
    queued: int
    policy: str

    @property
    def available(self) -> int:
        return max(0, self.max_running - self.running)


class SubagentExecutionCapacity:
    """``max_running`` 个槽位的异步 FIFO 准入控制器。

    控制器在 Gateway 启动时构造（``subagent_runtime.*`` 是 startup-only，§4.2），
    但 ``asyncio.Lock`` 是**第一次使用**时才绑到隔离循环上的：构造发生在父线程，
    真正的 acquire/release 全在隔离循环上。
    """

    def __init__(self, config: SubagentRuntimeConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._running = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @asynccontextmanager
    async def slot(self):
        """拿到槽位才算进入。拿不到就抛 ``AdmissionDenied``（调用方负责记账）。"""
        await self._acquire()
        try:
            yield
        finally:
            # shield：这个 finally 常常跑在"父 run 取消 / 子代理墙钟到点"的路径上，
            # 而 await 是取消的注入点。不 shield 的话槽位会随每次取消慢慢漏光。
            with suppress(asyncio.CancelledError):
                await asyncio.shield(self._release())

    async def _acquire(self) -> None:
        async with self._lock:
            if self._running < self._config.max_running:
                self._running += 1
                return
            if self._config.admission_policy != "queue" or self._config.max_queued <= 0:
                raise AdmissionDenied(
                    REASON_REJECTED,
                    f"all {self._config.max_running} subagent slots are busy and "
                    f"admission_policy is {self._config.admission_policy!r}",
                )
            if len(self._waiters) >= self._config.max_queued:
                raise AdmissionDenied(
                    REASON_QUEUE_FULL,
                    f"the subagent queue is full ({self._config.max_queued} waiting)",
                )
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        try:
            await asyncio.wait_for(waiter, timeout=self._config.queue_timeout_seconds)
        except TimeoutError:
            await self._abandon(waiter)
            raise AdmissionDenied(
                REASON_QUEUE_TIMEOUT,
                f"no subagent slot became free within {self._config.queue_timeout_seconds}s",
            ) from None
        except BaseException:
            # 取消（父 run 停了 / 任务被砍）也必须把队列位置还回去
            await self._abandon(waiter)
            raise

    async def _release(self) -> None:
        """把槽位交给队首等待者；没人等才真的减一。"""
        async with self._lock:
            while self._waiters:
                waiter = self._waiters.popleft()
                if waiter.cancelled():
                    continue  # 已经放弃的等待者不占位
                if not waiter.done():
                    waiter.set_result(None)  # 槽位所有权直接转交，_running 不变
                    return
            self._running -= 1
            if self._running < 0:  # 不该发生；夹住比让计数漂走好
                logger.error("subagent capacity went negative; clamping to 0")
                self._running = 0

    async def _abandon(self, waiter: asyncio.Future[None]) -> None:
        """超时 / 取消的收尾：摘掉队列位置；槽位**已经**交到我手上就还回去。"""
        handed_over = False
        async with self._lock:
            with suppress(ValueError):
                self._waiters.remove(waiter)
            handed_over = waiter.done() and not waiter.cancelled() and waiter.exception() is None
        if handed_over:
            with suppress(asyncio.CancelledError):
                await asyncio.shield(self._release())
        elif not waiter.done():
            waiter.cancel()

    def snapshot(self) -> CapacitySnapshot:
        """容量快照。可能从**非 loop 线程**读（配置热路径、健康检查）。

        所以**不要**迭代 ``_waiters``：``len(deque)`` 是原子的，迭代不是。超时之后还没来得及
        把自己从队列里摘掉的 waiting 会让这个数字**偏忙**——偏忙比偏闲安全（宁可少放一个，
        也不要在控制器已经满的时候再塞一个进去）。
        """
        return CapacitySnapshot(
            max_running=self._config.max_running,
            running=self._running,
            queued=len(self._waiters),
            policy=self._config.admission_policy,
        )


__all__ = [
    "REASON_QUEUE_FULL",
    "REASON_QUEUE_TIMEOUT",
    "REASON_REJECTED",
    "AdmissionDenied",
    "CapacitySnapshot",
    "SubagentExecutionCapacity",
]
