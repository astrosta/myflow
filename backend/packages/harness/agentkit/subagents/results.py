"""``SubagentResult`` 与进程内注册表（M8 §8）。

执行器（§7）跑在**隔离循环**上，父循环的 ``task`` 工具（§9）轮询这里的 holder：
两边唯一的共享状态就是这个对象，所以它的每一次写都要过 ``_state_lock``——
取消回调可能从别的线程进来（§8）。

两个 ID 绝对不能合并（§2）：

* ``task_id``（= ``execution_id``，服务端 ``uuid4``）：注册表主键、轮询、取消、沙箱租约
* ``external_task_id``（= provider 的 ``tool_call_id``）：ToolMessage / ``task_*`` SSE /
  持久化 metadata / 前端卡片

provider 的 ``tool_call_id`` **不保证跨 run 唯一**：拿它当所有权键，两个并发 run 撞上同一个
id 时会互相取消对方的子代理——只在高并发下偶发，事后从日志里几乎看不出来。
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SubagentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    #: 仅 ``task`` 工具父侧轮询预算耗尽时的**对外结果**；执行器永不写这个终态到 holder
    #: （子代理墙钟到点仍是 ``TIMED_OUT``）。
    POLLING_TIMED_OUT = "polling_timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset({
    SubagentStatus.COMPLETED,
    SubagentStatus.FAILED,
    SubagentStatus.CANCELLED,
    SubagentStatus.TIMED_OUT,
    SubagentStatus.POLLING_TIMED_OUT,
})

#: 执行侧 holder 终态 → 对外契约字符串（§9.3）。``POLLING_TIMED_OUT`` 不经这条表：
#: 它只由 ``task`` 工具在父侧放弃等待时直接产出。
EXTERNAL_STATUS_BY_RESULT_STATUS: dict[SubagentStatus, str] = {
    SubagentStatus.COMPLETED: SubagentStatus.COMPLETED.value,
    SubagentStatus.FAILED: SubagentStatus.FAILED.value,
    SubagentStatus.CANCELLED: SubagentStatus.CANCELLED.value,
    SubagentStatus.TIMED_OUT: SubagentStatus.TIMED_OUT.value,
}

#: ``try_set_terminal`` 允许携带的载荷。写错键就是编程错误，当场抛。
_TERMINAL_PAYLOAD_FIELDS = frozenset(
    {"result", "error", "stop_reason", "admission_failure", "citations"}
)


@dataclass
class SubagentResult:
    """一次委派的共享状态。第一次终态获胜（§2/§8）。"""

    task_id: str
    trace_id: str
    status: SubagentStatus
    external_task_id: str | None = None
    result: str | None = None
    #: 子代理最终回答里实际引用、且确实来自本次 knowledge_search 的来源清单。
    #: task 工具把它带给 lead，lead 的最终消息再按标记过滤一次。
    citations: list[dict] = field(default_factory=list)
    error: str | None = None
    stop_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage_records: list[dict] = field(default_factory=list)
    admission_failure: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    #: §14.1 捕获的步骤载荷（``{"message_index": ..., "message": {...}}``）。
    #: 捕获在隔离循环写，父循环的 ``task`` 轮询读——所以走同一把锁。
    steps: list[dict] = field(default_factory=list, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    #: §7.5：用量上报只能有一次——task 工具正常收尾和隔离循环的兜底交付会撞车。
    _usage_reported: bool = field(default=False, init=False, repr=False)

    def try_set_terminal(self, status: SubagentStatus, **payload: Any) -> bool:
        """第一次终态获胜：后台超时、父 run 取消、worker 正常结束会竞态写同一个 holder，
        晚到的写不得改 ``status`` 或载荷（§2）。非终态参数直接 ``ValueError``。
        """
        if not isinstance(status, SubagentStatus) or not status.is_terminal:
            raise ValueError(f"try_set_terminal() needs a terminal status, got {status!r}")
        unknown = set(payload) - _TERMINAL_PAYLOAD_FIELDS
        if unknown:
            raise ValueError(f"unknown terminal payload fields: {sorted(unknown)}")
        with self._state_lock:
            if self.status.is_terminal:
                return False
            self.status = status
            for key, value in payload.items():
                setattr(self, key, value)
            self.completed_at = datetime.now(UTC)
            return True

    def mark_running(self) -> bool:
        """准入通过、即将开跑。已经不是 PENDING（或已终态）就不动它。"""
        with self._state_lock:
            if self.status is not SubagentStatus.PENDING:
                return False
            self.status = SubagentStatus.RUNNING
            self.started_at = datetime.now(UTC)
            return True

    def update_token_usage_records(self, records: Sequence[Mapping] | None) -> None:
        """还在跑时发布**累计快照**，供下一条 ``task_running`` 携带（§15）。终态后忽略。

        是替换而不是追加：调用方持有的是"这个子代理到目前为止的总量"，
        追加会让同一次 LLM 调用被记两遍（一次回调可能打多次 ``on_llm_end``）。
        """
        snapshot = [dict(record) for record in (records or ())]
        with self._state_lock:
            if self.status.is_terminal:
                return
            self.token_usage_records = snapshot

    def claim_usage_report(self) -> bool:
        """先到先得：真正的上报者（``task`` 工具或隔离循环的兜底交付）拿到 True（§7.5）。"""
        with self._state_lock:
            if self._usage_reported:
                return False
            self._usage_reported = True
            return True

    def append_steps(self, steps: Sequence[Mapping]) -> None:
        """§14.1 每捕获一个新步骤就调一次（隔离循环）。"""
        if not steps:
            return
        with self._state_lock:
            self.steps.extend(dict(step) for step in steps)

    def pending_steps(self, reported: int) -> list[dict]:
        """从 ``reported`` 之后还没报出去的步骤（父循环轮询，§9.1 第 8 步）。"""
        with self._state_lock:
            return [dict(step) for step in self.steps[max(reported, 0) :]]


# ---------------------------------------------------------------- 进程内注册表
# 取消回调可能从别的线程进来 → 一把 threading.Lock，不用 asyncio.Lock。

_registry_lock = threading.Lock()
_registry: dict[str, SubagentResult] = {}
_run_index: dict[str, set[str]] = {}
_registry_futures: dict[str, Any] = {}


def register(result: SubagentResult, *, run_id: str | None = None) -> None:
    """登记一个 holder。``run_id`` 只喂给 ``cancel_all_for_run``（§17）。"""
    with _registry_lock:
        _registry[result.task_id] = result
        if run_id:
            _run_index.setdefault(run_id, set()).add(result.task_id)


def get(execution_id: str) -> SubagentResult | None:
    with _registry_lock:
        return _registry.get(execution_id)


def attach_future(execution_id: str, future: Any) -> None:
    """登记执行器提交到隔离循环的那个 future（§8）：取消时要连它一起取消。"""
    with _registry_lock:
        if execution_id in _registry:
            _registry_futures[execution_id] = future


def cleanup(execution_id: str) -> None:
    """终态之后摘掉条目（§8）。重复调用是幂等的。"""
    with _registry_lock:
        _registry.pop(execution_id, None)
        future = _registry_futures.pop(execution_id, None)
        for ids in _run_index.values():
            ids.discard(execution_id)
    del future  # 只是把它从注册表里摘掉；取消与否由调用方决定


def request_cancel(execution_id: str) -> bool:
    """设 ``cancel_event``，并取消已提交的 future。**两件事都在释放注册表锁之后**（§8）：

    取消可能同步触发完成回调，而回调会再拿同一把锁 —— 在锁里调就是死锁。
    协作式取消（``cancel_event``）是执行器在 chunk 边界检查的那个；
    ``future.cancel()`` 只是把还在排队的执行直接掐掉。
    """
    with _registry_lock:
        holder = _registry.get(execution_id)
        future = _registry_futures.get(execution_id)
    if holder is None:
        return False
    holder.cancel_event.set()
    if future is not None:
        with suppress(Exception):
            future.cancel()
    return True


def cancel_all_for_run(run_id: str | None) -> list[str]:
    """父 run 取消时扇出（§17），返回被取消的 ``execution_id``。

    ``run_id`` 缺失时**什么也不取消**：进程里可能还有别的 run 的子代理在跑，
    猜一个"全都取消"会误伤别人的委派。调用方（worker）手里一定拿得到 run_id。
    """
    if not run_id:
        logger.warning("cancel_all_for_run() called without a run_id; refusing to touch other runs")
        return []
    with _registry_lock:
        execution_ids = [item for item in _run_index.get(run_id, ()) if item in _registry]
    for execution_id in execution_ids:
        request_cancel(execution_id)
    return execution_ids


#: §17：父 run 收尾时等隔离循环清理子代理的上限。等不到也让收尾继续——run 的终态比清理更要紧。
SUBAGENT_CLEANUP_TIMEOUT_SECONDS = 5.0


def future_for(execution_id: str) -> Any | None:
    """这个 holder 的执行 future（隔离循环上的那个）。没有就是还没提交 / 已经摘掉。"""
    with _registry_lock:
        return _registry_futures.get(execution_id)


def holders_for_run(run_id: str | None) -> list[SubagentResult]:
    """这次 run 还挂在注册表上的 holder（§17 收尾要按它们算「谁还没结束」）。"""
    if not run_id:
        return []
    with _registry_lock:
        return [_registry[item] for item in _run_index.get(run_id, ()) if item in _registry]


def run_futures(run_id: str | None) -> list[Any]:
    """这次 run 还在注册表里的执行 future（§17 等它们收尾）。"""
    if not run_id:
        return []
    with _registry_lock:
        return [
            _registry_futures[item]
            for item in _run_index.get(run_id, ())
            if item in _registry_futures
        ]


def wait_for_run_cleanup(
    run_id: str | None, *, timeout: float = SUBAGENT_CLEANUP_TIMEOUT_SECONDS
) -> tuple[int, int]:
    """等隔离循环把这次 run 的子代理清理完（§17）。返回 ``(settled, pending)``。

    **同步**函数：调用方（worker 的异步 ``finally``）必须用 ``asyncio.to_thread`` 调它。
    直接 await 一个阻塞在这里的调用会把父循环堵住，而它等的那几件事（沙箱排空、租约释放、
    用量的回程 ``call_soon_threadsafe``）恰恰要在父循环上跑。

    已经取消 / 已经失败的 future 都算「收尾完成」：这里要的是「它不再动」，不是「它成功」。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    settled = pending = 0
    for future in run_futures(run_id):
        try:
            future.result(timeout=max(0.0, deadline - time.monotonic()))
            settled += 1
        except concurrent.futures.TimeoutError:
            pending += 1
        except (concurrent.futures.CancelledError, Exception):
            settled += 1  # CancelledError 是 BaseException，必须显式写进来，否则会漏到调用方
    return settled, pending


def reset_registry() -> None:
    """测试与进程关停用：清空注册表，**不**取消任何东西。"""
    with _registry_lock:
        _registry.clear()
        _run_index.clear()
        _registry_futures.clear()
