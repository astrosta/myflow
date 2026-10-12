"""SQL run history. Each method opens a short session and commits it."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from agentkit.persistence.base import decode_keyset_cursor, encode_keyset_cursor, iso_utc, row_to_dict
from agentkit.persistence.json_compat import json_value_matches
from agentkit.persistence.run.model import RunRow
from agentkit.runtime.runs.schemas import RunStatus
from agentkit.persistence.run.base import ACTIVE_STATUSES, RunStore
from agentkit.runtime.user_context import AUTO, resolve_user_id

TERMINAL_STATUSES = ("success", "error", "timeout", "interrupted")
_PREVIEW_LIMIT = 2000
_MODEL_NAME_LIMIT = 128
_DATETIME_COLUMNS = ("created_at", "updated_at", "lease_expires_at", "cancel_requested_at")


def _encode_cursor(row: RunRow) -> str:
    return encode_keyset_cursor("desc", iso_utc(row.created_at) or "", row.run_id)


def _parse_cursor(cursor: str, *, order: str = "desc") -> tuple[datetime, str]:
    created_at, run_id = decode_keyset_cursor(cursor, order=order)
    return datetime.fromisoformat(created_at.replace("Z", "+00:00")), run_id


def _fit_model_name(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()[:_MODEL_NAME_LIMIT]


def _truncate_previews(values: Mapping[str, Any]) -> dict[str, Any]:
    fitted = dict(values)
    for key in ("first_human_message", "last_ai_message"):
        text = fitted.get(key)
        if isinstance(text, str):
            fitted[key] = text[:_PREVIEW_LIMIT]
    return fitted


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


class SqlRunStore(RunStore):
    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    def _to_row_kwargs(self, row: Mapping[str, Any], kind: Any) -> dict[str, Any]:
        data = dict(row)
        if "metadata" in data:
            data["metadata_json"] = data.pop("metadata")
        if "kwargs" in data:
            data["kwargs_json"] = data.pop("kwargs")
        data["operation_kind"] = getattr(kind, "value", kind)
        if "model_name" in data:
            data["model_name"] = _fit_model_name(data["model_name"])
        data = _truncate_previews(data)
        for key in _DATETIME_COLUMNS:
            if key not in data:
                continue
            coerced = _as_datetime(data[key])
            if coerced is None:
                data.pop(key)
            else:
                data[key] = coerced
        allowed = {column.key for column in RunRow.__table__.columns}
        return {key: value for key, value in data.items() if key in allowed}

    def _row_to_dict(self, row: RunRow) -> dict[str, Any]:
        return row_to_dict(row)

    async def create_thread_operation_atomic(
        self, row: dict, *, kind, supersede_active: bool = False
    ) -> bool:
        try:
            async with self._sf() as session:
                if supersede_active:
                    active = (
                        await session.scalars(
                            select(RunRow).where(
                                RunRow.thread_id == row["thread_id"],
                                RunRow.status.in_(tuple(ACTIVE_STATUSES)),
                            )
                        )
                    ).all()
                    if any(item.operation_kind != "run" for item in active):
                        return False
                    now = datetime.now(UTC)
                    for item in active:
                        item.status = "interrupted"
                        item.updated_at = now
                    await session.flush()
                session.add(RunRow(**self._to_row_kwargs(row, kind)))
                await session.commit()
            return True
        except IntegrityError:
            return False

    async def start_run(self, run_id: str) -> bool:
        """pending → running。唯一入库入口；返回 False 表示行已经不是 pending。"""
        async with self._sf() as session:
            result = await session.execute(
                update(RunRow)
                .where(RunRow.run_id == run_id, RunRow.status == "pending")
                .values(status="running", updated_at=datetime.now(UTC))
            )
            await session.commit()
            return bool(result.rowcount)

    async def update_run_progress(self, run_id: str, **totals: Any) -> None:
        """WHERE status='running'。CAS 落空就静默：run 已终态，进度数字不再有意义。"""
        async with self._sf() as session:
            await session.execute(
                update(RunRow)
                .where(RunRow.run_id == run_id, RunRow.status == "running")
                .values(**_truncate_previews(totals), updated_at=datetime.now(UTC))
            )
            await session.commit()

    async def update_run_completion(
        self, run_id: str, *, status: str, error: str | None = None, **totals: Any
    ) -> bool:
        """终态 + token 汇总一次写完。同一个终态可以写两遍，换成另一个终态被拒。"""
        values = _truncate_previews(totals)
        values.update(status=status, error=error, updated_at=datetime.now(UTC))
        async with self._sf() as session:
            result = await session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == run_id,
                    or_(RunRow.status.not_in(TERMINAL_STATUSES), RunRow.status == status),
                )
                .values(**values)
            )
            await session.commit()
            return bool(result.rowcount)

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id=AUTO,
        limit: int = 100,
        before: str | None = None,
        order: str = "desc",
        statuses: frozenset | None = None,
        metadata: dict | None = None,
    ) -> list[dict]:
        stmt = select(RunRow).where(RunRow.thread_id == thread_id, RunRow.operation_kind == "run")
        if (uid := resolve_user_id(user_id, method_name="RunStore.list_by_thread")) is not None:
            stmt = stmt.where(RunRow.user_id == uid)
        if statuses is not None:
            allowed = [getattr(item, "value", item) for item in statuses]
            stmt = stmt.where(RunRow.status.in_(allowed))
        for key, expected in (metadata or {}).items():
            stmt = stmt.where(json_value_matches(RunRow.metadata_json, key, expected))
        if before is not None:
            created_at, run_id = _parse_cursor(before, order=order)
            key = tuple_(RunRow.created_at, RunRow.run_id)
            bound = (created_at, run_id)
            stmt = stmt.where(key < bound if order == "desc" else key > bound)
        if order == "asc":
            stmt = stmt.order_by(RunRow.created_at.asc(), RunRow.run_id.asc())
        else:
            stmt = stmt.order_by(RunRow.created_at.desc(), RunRow.run_id.desc())
        stmt = stmt.limit(limit)
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        page = []
        for row in rows:
            body = self._row_to_dict(row)
            body["cursor"] = encode_keyset_cursor(order, iso_utc(row.created_at) or "", row.run_id)
            page.append(body)
        return page

    async def update_status(
        self, run_id: str, status: RunStatus, *, error: str | None = None, stop_reason: str | None = None
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(RunRow)
                .where(RunRow.run_id == run_id, RunRow.status.in_(tuple(ACTIVE_STATUSES)))
                .values(
                    status=getattr(status, "value", status),
                    error=error,
                    stop_reason=stop_reason,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def get(self, run_id: str, *, user_id: str | None) -> dict | None:
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None or (user_id is not None and row.user_id != user_id):
                return None
            return self._row_to_dict(row)

    async def list_inflight(self) -> list[dict]:
        statement = select(RunRow).where(RunRow.status.in_(tuple(ACTIVE_STATUSES)))
        async with self._sf() as session:
            rows = (await session.scalars(statement)).all()
        return [self._row_to_dict(row) for row in rows]

    async def get_by_idempotency_key(self, scoped_key: str) -> dict | None:
        statement = select(RunRow).where(RunRow.idempotency_key == scoped_key)
        async with self._sf() as session:
            row = await session.scalar(statement)
        return self._row_to_dict(row) if row is not None else None

    async def delete_thread_operation(self, run_id: str, *, user_id: str | None) -> None:
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None or (user_id is not None and row.user_id != user_id):
                return
            if row.operation_kind == "run":
                raise ValueError(f"delete_thread_operation refuses to delete run history for {run_id}")
            await session.execute(sa_delete(RunRow).where(RunRow.run_id == run_id))
            await session.commit()
