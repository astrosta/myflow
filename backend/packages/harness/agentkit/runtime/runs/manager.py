"""
RunManager、RunRecord、状态机
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from agentkit.runtime.runs.schemas import (
    DisconnectMode,
    RunStartOutcome,
    RunStartupError,
    RunStatus,
    ThreadOperationKind,
)
from agentkit.runtime.runs.store import RunStore

RUN_RECORD_RETENTION_SECONDS = 300.0
_ACTIVE = frozenset({RunStatus.pending, RunStatus.running})
_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.pending: frozenset({RunStatus.running, RunStatus.interrupted, RunStatus.error}),
    RunStatus.running: frozenset({RunStatus.success, RunStatus.error, RunStatus.timeout, RunStatus.interrupted}),
}
_STRATEGIES = frozenset({"reject", "interrupt", "rollback"})

@dataclass
class RunRecord:
    # --- 第一组：可序列化，会进 RunStore ---
    run_id: str
    thread_id: str
    assistant_id: str | None
    status: RunStatus
    on_disconnect: DisconnectMode
    operation_kind: ThreadOperationKind = ThreadOperationKind.run
    multitask_strategy: str = "reject"
    metadata: dict = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)      # {"input": ..., "config": ...}，密钥已脱敏
    user_id: str | None = None
    model_name: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    idempotency_key: str | None = None

    # --- 第二组：进程内控制，绝不序列化 ---
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"
    start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    finalizing: bool = False                        # 见 §5.2：不是状态机上的一档
    store_only: bool = False                        # 从 store hydrate 出来的，没有本地 task
    idempotency_reused: bool = False                # 见 §5.7：这个 caller 不许再挂 worker

    # --- 第三组：统计 ---
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0                      # 归因维度，见下
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    # --- M9 预留，M3 恒为默认值 ---
    owner_worker_id: str | None = None
    lease_expires_at: str | None = None
    ownership_lost: bool = False
    stop_reason: str | None = None


def _record_to_row(record: RunRecord) -> dict:
    """只取第一组和第三组字段；StrEnum 存成普通字符串。"""
    return {
        "run_id": record.run_id,
        "thread_id": record.thread_id,
        "assistant_id": record.assistant_id,
        "status": record.status.value,
        "on_disconnect": record.on_disconnect.value,
        "operation_kind": record.operation_kind.value,
        "multitask_strategy": record.multitask_strategy,
        "metadata": record.metadata,
        "kwargs": record.kwargs,
        "user_id": record.user_id,
        "model_name": record.model_name,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "idempotency_key": record.idempotency_key,
        "total_input_tokens": record.total_input_tokens,
        "total_output_tokens": record.total_output_tokens,
        "llm_call_count": record.llm_call_count,
        "lead_agent_tokens": record.lead_agent_tokens,
        "subagent_tokens": record.subagent_tokens,
        "middleware_tokens": record.middleware_tokens,
        "token_usage_by_model": record.token_usage_by_model,
    }


def _record_from_row(row: dict) -> RunRecord:
    """第二组（task、abort_event、start_lock…）全部是新建的空值，并标 store_only=True。"""
    return RunRecord(
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        assistant_id=row.get("assistant_id"),
        status=RunStatus(row.get("status") or RunStatus.pending),
        on_disconnect=DisconnectMode(row.get("on_disconnect") or DisconnectMode.cancel),
        operation_kind=ThreadOperationKind(row.get("operation_kind") or ThreadOperationKind.run),
        metadata=row.get("metadata") or {},
        kwargs=row.get("kwargs") or {},
        user_id = row.get("user_id"),
        model_name = row.get("model_name"),
        error = row.get("error"),
        created_at = row.get("created_at"),
        updated_at = row.get("updated_at"),
        idempotency_key = row.get("idempotency_key"),
        total_input_tokens = row.get("total_input_tokens"),
        total_output_tokens = row.get("total_output_tokens"),
        llm_call_count = row.get("llm_call_count"),
        lead_agent_tokens = row.get("lead_agent_tokens"),
        subagent_tokens = row.get("subagent_tokens"),
        middleware_tokens = row.get("middleware_tokens"),
        token_usage_by_model = row.get("token_usage_by_model"),
        store_only=True,
    )


def _transition_locked(record: RunRecord, target: RunStatus, *, error: str | None = None) -> bool:
    """改内存状态。返回 False 表示记录已是终态——竞态里的输家，正常结果。"""
    if record.status not in _ACTIVE:
        return False
    if target not in _ALLOWED_TRANSITIONS[record.status]:
        raise InvalidRunTransition(f"{record.run_id}: {record.status} -> {target}")
    record.status = target
    if error is not None:
        record.error = error
    record.updated_at = now_iso()
    return True

def _abort_locked(record: RunRecord, *, action: str) -> None:
    """顶替（§5.4 第 5 步）和取消（§5.9）共用的内存侧动作。store 行由调用方负责：
    顶替时已在准入第 3 步的同一个原子操作里标好，取消时在锁外补写。"""
    was_running = record.status is RunStatus.running
    if not _transition_locked(record, RunStatus.interrupted):
        return                                   # 已是终态、只是还在收尾：不用再管
    record.abort_action = action                 # "interrupt" / "rollback"，worker 在 finally 里照做
    record.abort_event.set()
    if record.finalizing:
        return
    record.finalizing = record.task is not None and not record.task.done()
    if was_running and record.finalizing:
        record.task.cancel()    

class RunManager:
    def __init__(self, store: RunStore, *, retention_seconds: float = RUN_RECORD_RETENTION_SECONDS) -> None:
        self._store = store
        self._retention_seconds = retention_seconds
        self._runs: dict[str, RunRecord] = {}
        self._runs_by_thread: dict[str, dict[str, None]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closing = False
    
    async def get(self, run_id: str, *, user_id: str | None) -> RunRecord | None:
        """从缓存和store里获取RunRecord，若不存在返回None"""
        async with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            row = await self._store.get(run_id, user_id=user_id)
            async with self._lock:
                record = self._runs.get(run_id) #store往返期间可能刚登记了同一个run，重新确认
            if record is None:
                return _record_from_row(row) if row is not None else None
        return record if user_id is None or record.user_id == user_id else None

    def _index_run_locked(self, record: RunRecord) -> None:
        self._runs[record.run_id] = record
        self._runs_by_thread.setdefault(record.thread_id, {})[record.run_id] = None

    def _unindex_run_locked(self, record: RunRecord) -> None:
        self._runs.pop(record.run_id, None)
        bucket = self._runs_by_thread.get(record.thread_id)
        if bucket is not None:
            bucket.pop(record.run_id, None)
            if not bucket:
                del self._runs_by_thread[record.thread_id]
    
    def _thread_records_locked(self, thread_id: str) -> list[RunRecord]:
        return [self._runs[run_id] for run_id in self._runs_by_thread.get(thread_id, ())]
        
    async def list_by_thread(self, thread_id: str, *, user_id: str | None, limit: int = 100) -> list[RunRecord]:
        rows = await self._store.list_by_thread(thread_id, user_id=user_id, limit=limit)
        async with self._lock:
            live = {record.run_id: record for record in self._thread_records_locked(thread_id)}
        return [live.get(row["run_id"]) or _record_from_row(row) for row in rows]

    @asynccontextmanager
    async def reserve_thread_operation(
        self, thread_id: str, *, kind: ThreadOperationKind, user_id: str | None
    ) -> AsyncIterator[RunRecord]:
        if kind is ThreadOperationKind.run:
            raise ValueError("normal runs must be admitted with create_or_reject()")
        record = await self._admit(
            thread_id, None, kind=kind, multitask_strategy="reject", user_id=user_id,
            on_disconnect=DisconnectMode.continue_,          # 预留没有 SSE 连接，这个字段只是为了填满必填项
        )
        try:
            yield record
        finally:
            try:
                await self._store.delete_thread_operation(record.run_id, user_id=record.user_id)
            except Exception:
                logger.warning("Failed to release thread operation %s", record.run_id, exc_info=True)
            finally:
                async with self._lock:
                    self._unindex_run_locked(record)


    async def has_inflight(self, thread_id: str) -> bool:
        async with self._lock:
            return any(
                r.operation_kind is ThreadOperationKind.run and (r.status in _ACTIVE or r.finalizing)
                for r in self._thread_records_locked(thread_id)
            )

    async def create_or_reject(
        self,
        thread_id: str,
        assistant_id: str | None,
        *,
        run_id: str | None = None,                 # start_run 预分配（§4.5）；缺省时这里生成
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        multitask_strategy: str = "reject",
        metadata: dict | None = None,
        kwargs: dict | None = None,                # 脱敏后的原始请求（§4.5）
        model_name: str | None = None,
        user_id: str | None = None,
        idempotency_key: str | None = None,        # 已加 scope（§4.5）
    ) -> RunRecord:
        return await self._admit(
            thread_id, assistant_id, kind=ThreadOperationKind.run, run_id=run_id,
            on_disconnect=on_disconnect, multitask_strategy=multitask_strategy,
            metadata=metadata or {}, kwargs=kwargs or {}, model_name=model_name,
            user_id=user_id, idempotency_key=idempotency_key,
        )

    async def _admit(self, thread_id, assistant_id, *, kind, run_id=None, multitask_strategy="reject",
                 idempotency_key=None, user_id=None, **fields) -> RunRecord:
        if multitask_strategy not in _STRATEGIES:
            raise UnsupportedStrategyError(multitask_strategy)
        now = now_iso()
        record = RunRecord(
            run_id=run_id or new_run_id(), thread_id=thread_id, assistant_id=assistant_id,
            status=RunStatus.pending, operation_kind=kind, multitask_strategy=multitask_strategy,
            user_id=user_id, idempotency_key=idempotency_key, created_at=now, updated_at=now, **fields,
        )
        supersede = multitask_strategy != "reject"

        async with self._lock:
            # 先获取幂等record，命中直接返回
            if idempotency_key is not None:
                if (reused := await self._find_idempotency_key(idempotency_key, thread_id, user_id)) is not None:
                    return reused

            if self._closing:
                raise ConflictError("Gateway is shutting down")

            occupants = {r for r in self._thread_records_locked(thread_id) if r.status in _ACTIVE or r.finalizing}
            if occupants and not supersede:
                raise ConflictError(f"Thread {thread_id} already has an active operation")
            if any(r.operation_kind is not ThreadOperationKind.run for r in occupants):
                raise ConflictError(f"Thread {thread_id} has an active non-run operation")

            # store抢位
            admitted = await self._store.create_thread_operation_atomic(
                _record_to_row(record),
                kind=kind,
                supersede_active=supersede,
            )
            if not admitted:
                if idempotency_key is not None:
                    if (reused := await self._find_idempotent_locked(idempotency_key, thread_id, user_id)) is not None:
                        return reused              # 另一个 worker 拿同一个键先到了一步
                raise ConflictError(f"Thread {thread_id} already has an active operation")

            # 登记内存
            self._index_run_locked(record)

            # 顶替旧 run：只改内存，不 await（§5.5）
            for old in occupants:
                _abort_locked(old, action=multitask_strategy)

        return record

    async def _find_idempotent_locked(self, key: str, thread_id: str, user_id: str | None) -> RunRecord | None:
        existing = next((r for r in self._runs.values() if r.idempotency_key == key), None)
        if existing is None and (row := await self._store.get_by_idempotency_key(key)) is not None:
            existing = _record_from_row(row)
        if existing is None:
            return None
        if existing.thread_id != thread_id or existing.user_id != user_id:
            raise RuntimeError("idempotency key resolved to a different thread or user")   # scope 保证不会发生
        existing.idempotency_reused = True
        return existing

    async def try_start(self, run_id: str) -> RunStartOutcome:
        """try_start 只做一件事：pending -> running"""
        async with self._lock:
            record = self._runs.get(run_id)
        if record is None:
            return RunStartupError(f"Cannot start unknown run {run_id}")
        async with record.start_lock:
            async with self._lock:
                if record.abort_event.is_set() or record.status is not RunStatus.pending: # 不是pending状态，可能被其他worker执行了
                    return RunStartOutcome.cancelled
            try:
                started = await self._store.start_run(run_id)        # ② CAS，在管理器锁外
            except Exception as exc:
                raise RunStartupError(f"Failed to start run {run_id}") from exc

            async with self._lock:
                if not started:
                    _transition_locked(record, RunStatus.interrupted)
                    record.abort_event.set()
                    return RunStartOutcome.cancelled

                if started and not record.abort_event.is_set():
                    _transition_locked(record, RunStatus.running)
                    return RunStartOutcome.started
            
            await self._store.update_status(run_id, RunStatus.interrupted)
            return RunStartOutcome.cancelled

    async def fail_start_if_pending(self, run_id: str, *, error: str) -> bool:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status is not RunStatus.pending:
                return False                         # 已经启动的交给 worker 自己的收尾路径
            _transition_locked(record, RunStatus.error, error=error)
            record.abort_event.set()                 # 随后的 try_start 走 cancelled
        await self._store.update_status(run_id, RunStatus.error, error=error)
        return True
    
    async def wait_for_prior_finalizing(
        self, thread_id: str, run_id: str, *, abort_event: asyncio.Event | None = None, poll_interval: float = 0.01
    ) -> None:
        while True:
            async with self._lock:
                prior: list[RunRecord] = []
                for record in self._thread_records_locked(thread_id):
                    if record.run_id == run_id:
                        break
                    prior.append(record)
                else:
                    return                           # 自己已不在登记表里，没什么可等
                if not any(r.finalizing for r in prior):
                    return
            if abort_event is not None and abort_event.is_set():
                return                               # 自己被取消了就别等，交给 try_start 去拒
            await asyncio.sleep(poll_interval)

    async def update_run_progress(self, run_id: str, totals: RunTotals) -> None:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None or record.status is not RunStatus.running:
                return
            _apply_totals(record, totals)
        await self._store.update_run_progress(run_id, **asdict(totals))

    async def complete(
        self, run_id: str, *, status: RunStatus, error: str | None = None, totals: RunTotals | None = None
    ) -> bool:
        """worker 收尾的唯一出口。返回 False 表示终态已经被别人定了（多半是取消）。"""
        if status in _ACTIVE:
            raise ValueError(f"complete() needs a terminal status, got {status}")
            
        async with self._lock:
            record = self._runs[run_id]
            won = _transition_locked(record, status, error=error)
            if totals is not None:
                _apply_totals(record, totals)
            final_status, final_error = record.status, record.error  

        written = await self._store.update_run_completion(
            run_id, status=final_status, error=final_error, **(asdict(totals) if totals else {}),
        )
        return won and written

    async def cancel(self, run_id: str, *, action: Literal["interrupt", "rollback"] = "interrupt") -> CancelOutcome:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is not None:
                if record.status is RunStatus.interrupted and record.abort_event.is_set():
                    return CancelOutcome.cancelled                   # 重复取消：幂等
                if record.status not in _ACTIVE:
                    return CancelOutcome.not_cancellable
                _abort_locked(record, action=action)                 # 和顶替旧 run 是同一件事（§5.5）
        if record is None:
            row = await self._store.get(run_id, user_id=None)        # 属主已由路由用 get(..., user_id=) 查过
            if row is None:
                return CancelOutcome.unknown
            return CancelOutcome.not_active_locally if row["status"] in ACTIVE_STATUSES else CancelOutcome.not_cancellable
        await self._store.update_status(run_id, RunStatus.interrupted)

        return CancelOutcome.cancelled

    async def shutdown(self, *, timeout: float) -> None:
        """§6.3 的第一步：拒新准入，再让 run 自己停，超时再强制。之后 lifespan 才能关 checkpointer。"""
        async with self._lock:
            self._closing = True
            live = [r for r in self._runs.values() if r.status in _ACTIVE and r.task is not None and not r.task.done()]
            for record in live:
                record.abort_action = "interrupt"
                record.abort_event.set()
                if record.status is RunStatus.running:
                    record.task.cancel()                             # pending 只置 abort（§4.6）
        if live:
            _, stuck = await asyncio.wait([r.task for r in live], timeout=timeout)
            for task in stuck:
                task.cancel()                                        # 强制：§4.6 说的唯一例外
            if stuck:
                await asyncio.wait(stuck, timeout=1.0)
        for task in list(self._cleanup_tasks):
            task.cancel()

    def schedule_cleanup(self, run_id: str) -> None:
        task = asyncio.create_task(
            self._cleanup_after(run_id, self._retention_seconds),
            context=contextvars.Context(),           # 空 context：不攥住请求的 user / trace
        )
        self._cleanup_tasks.add(task)                # 事件循环只弱引用 task，这里留强引用
        task.add_done_callback(self._cleanup_tasks.discard)


    async def _cleanup_after(self, run_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            if record.status in _ACTIVE or record.finalizing:
                logger.warning("Refusing to clean up active run %s", run_id)
                return
            self._unindex_run_locked(record)
    
    async def has_later_run(self, thread_id: str, run_id: str) -> bool:
        async with self._lock:
            run_ids = [r.run_id for r in self._thread_records_locked(thread_id) if r.operation_kind is ThreadOperationKind.run]
        return run_id in run_ids and run_ids[-1] != run_id

class ConflictError(Exception):
    """Raised when multitask_strategy=reject and thread has inflight runs."""

class UnsupportedStrategyError(Exception):
    """Raised when a multitask_strategy value is not yet implemented."""    