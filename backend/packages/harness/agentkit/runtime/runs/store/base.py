import abc
from typing import Any

from agentkit.runtime.runs.schemas import RunStatus, ThreadOperationKind

ACTIVE_STATUSES = frozenset({"pending", "running"})


class RunStore(abc.ABC):
    # --- 准入：唯一一个必须原子的方法 ---
    @abc.abstractmethod
    async def create_thread_operation_atomic(
        self, row: dict, *, kind: ThreadOperationKind, supersede_active: bool = False
    ) -> bool:
        """在 (thread_id) WHERE status IN ('pending','running') 的部分唯一索引上抢位。

        supersede_active=True（interrupt / rollback）：先把该 thread 上活跃的 run 行标成 interrupted
        再插入，两步在同一个原子操作里；活跃行里有非 run 的预留时不顶替，返回 False。
        返回 False 表示没抢到位（thread 忙，或幂等键已被占）。不要抛 IntegrityError 给上层。"""

    # --- 状态迁移：全部是对活跃行的 CAS ---
    @abc.abstractmethod
    async def start_run(self, run_id: str) -> bool:
        """pending -> running。返回 False 表示已不是 pending（多半被取消了）。"""

    @abc.abstractmethod
    async def update_status(
        self, run_id: str, status: RunStatus, *, error: str | None = None, stop_reason: str | None = None
    ) -> bool:
        """WHERE status IN ('pending','running')。返回 False 表示行已是终态或不存在。"""

    @abc.abstractmethod
    async def update_run_progress(self, run_id: str, **totals: Any) -> None:
        """WHERE status = 'running'。CAS 落空就静默返回：run 已终态，进度数字不再有意义。"""

    @abc.abstractmethod
    async def update_run_completion(
        self, run_id: str, *, status: RunStatus, error: str | None = None, **totals: Any
    ) -> bool:
        """终态 + token 汇总一次写完。WHERE status IN ('pending','running') OR status = :status：
        同一个终态可以写两遍（崩溃重试），换成另一个终态被拒，返回 False。"""

    # --- 读：user_id 没有默认值 ---
    @abc.abstractmethod
    async def get(self, run_id: str, *, user_id: str | None) -> dict | None: ...

    @abc.abstractmethod
    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None,
        limit: int = 100,
        before: str | None = None,
        order: str = "desc",
        statuses: frozenset | None = None,
        metadata: dict | None = None,
    ) -> list[dict]:
        """只返回 operation_kind='run' 的行。每行多一个 cursor。"""

    @abc.abstractmethod
    async def list_inflight(self) -> list[dict]:
        """启动时回收孤儿用：所有 pending/running 的行，不按 user_id 过滤。"""

    @abc.abstractmethod
    async def get_by_idempotency_key(self, scoped_key: str) -> dict | None: ...

    # --- 删 ---
    @abc.abstractmethod
    async def delete_thread_operation(self, run_id: str, *, user_id: str | None) -> None:
        """释放非 run 的短命预留行。对 operation_kind='run' 的行调用要抛 ValueError。"""

    # --- M9 预留：M3 不实现，但签名现在定 ---
    # renew_lease / claim_for_takeover / request_cancel /
    # finalize_if_not_cancelled / list_inflight_with_expired_lease