import copy
from datetime import UTC, datetime

from agentkit.runtime.runs.schemas import RunStatus, ThreadOperationKind
from agentkit.persistence.run.base import ACTIVE_STATUSES, RunStore

_TERMINAL = frozenset({"success", "error", "timeout", "interrupted"})


def _status(value) -> str:
    return getattr(value, "value", value)


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

    async def start_run(self, run_id: str) -> bool:
        row = self._rows.get(run_id)
        if row is None or row["status"] != "pending":
            return False
        row["status"] = "running"
        row["updated_at"] = datetime.now(UTC).isoformat()
        return True

    async def update_status(
        self, run_id: str, status: RunStatus, *, error: str | None = None, stop_reason: str | None = None
    ) -> bool:
        row = self._rows.get(run_id)
        if row is None or row["status"] not in ACTIVE_STATUSES:
            return False
        row["status"] = _status(status)
        row["error"] = error
        row["stop_reason"] = stop_reason
        row["updated_at"] = datetime.now(UTC).isoformat()
        return True

    async def update_run_progress(self, run_id: str, **totals) -> None:
        row = self._rows.get(run_id)
        if row is None or row["status"] != "running":
            return
        row.update(totals)

    async def update_run_completion(self, run_id: str, *, status, error: str | None = None, **totals) -> bool:
        row = self._rows.get(run_id)
        if row is None:
            return False
        target = _status(status)
        current = row["status"]
        if current in _TERMINAL and current != target:
            return False
        if current not in ACTIVE_STATUSES and current != target:
            return False
        row.update(totals)
        row["status"] = target
        row["error"] = error
        row["updated_at"] = datetime.now(UTC).isoformat()
        return True

    async def get(self, run_id, *, user_id):
        row = self._rows.get(run_id)
        if row is None or (user_id is not None and row.get("user_id") != user_id):
            return None
        return copy.deepcopy(row)

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
        from agentkit.persistence.base import decode_keyset_cursor, encode_keyset_cursor
        from agentkit.persistence.json_compat import python_value_matches

        rows = [
            row
            for row in self._rows.values()
            if row["thread_id"] == thread_id and row.get("operation_kind") == ThreadOperationKind.run
        ]
        if user_id is not None:
            rows = [row for row in rows if row.get("user_id") == user_id]
        if statuses is not None:
            allowed = {getattr(item, "value", item) for item in statuses}
            rows = [row for row in rows if str(getattr(row.get("status"), "value", row.get("status"))) in allowed]
        if metadata:
            rows = [
                row
                for row in rows
                if all(python_value_matches((row.get("metadata") or {}).get(key), expected) for key, expected in metadata.items())
            ]
        reverse = order != "asc"
        rows.sort(key=lambda row: (row.get("created_at") or "", row.get("run_id") or ""), reverse=reverse)
        if before is not None:
            created_at, run_id = decode_keyset_cursor(before, order=order)
            if order == "desc":
                rows = [row for row in rows if (row.get("created_at") or "", row.get("run_id") or "") < (created_at, run_id)]
            else:
                rows = [row for row in rows if (row.get("created_at") or "", row.get("run_id") or "") > (created_at, run_id)]
        page = []
        for row in rows[:limit]:
            copied = copy.deepcopy(row)
            copied["cursor"] = encode_keyset_cursor(order, str(copied.get("created_at") or ""), copied["run_id"])
            page.append(copied)
        return page

    async def list_inflight(self) -> list[dict]:
        return [copy.deepcopy(row) for row in self._rows.values() if row["status"] in ACTIVE_STATUSES]

    async def get_by_idempotency_key(self, scoped_key: str) -> dict | None:
        run_id = self._run_id_by_idempotency_key.get(scoped_key)
        if run_id is None:
            return None
        return copy.deepcopy(self._rows[run_id])

    async def delete_thread_operation(self, run_id: str, *, user_id: str | None) -> None:
        row = self._rows.get(run_id)
        if row is None or (user_id is not None and row.get("user_id") != user_id):
            return
        if row.get("operation_kind") == ThreadOperationKind.run:
            raise ValueError(f"delete_thread_operation refuses to delete run history for {run_id}")
        self._rows.pop(run_id, None)
        key = row.get("idempotency_key")
        if key is not None and self._run_id_by_idempotency_key.get(key) == run_id:
            del self._run_id_by_idempotency_key[key]
