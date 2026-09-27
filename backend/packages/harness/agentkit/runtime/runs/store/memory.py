
class MemoryRunStore(RunStore):
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._run_id_by_idempotency_key: dict[str, str] = {}

    async def create_thread_operation_atomic(self, row, *, kind, supersede_active=False) -> bool:
        # 函数体内没有 await，事件循环不会在中间切走——这就是全部的原子性。
        # 以后谁要在这里加 await，必须先加一把锁。
        key = row.get("idempotency_key")
        if key is not None and key in self._run_id_by_idempotency_key:
            return False
        active = [r for r in self._rows.values() if r["thread_id"] == row["thread_id"] and r["status"] in ACTIVE_STATUSES]
        if active:
            if not supersede_active or any(r["operation_kind"] != ThreadOperationKind.run for r in active):
                return False
            for r in active:
                r["status"], r["updated_at"] = RunStatus.interrupted.value, row["created_at"]
        self._rows[row["run_id"]] = {**row, "operation_kind": kind.value}
        if key is not None:
            self._run_id_by_idempotency_key[key] = row["run_id"]
        return True

    async def get(self, run_id, *, user_id):
        row = self._rows.get(run_id)
        if row is None or (user_id is not None and row.get("user_id") != user_id):
            return None
        return copy.deepcopy(row)