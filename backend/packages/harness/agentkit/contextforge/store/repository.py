"""SQL store for the ContextForge tool catalog.

The store owns the invariants the rest of the runtime relies on, so callers
cannot accidentally publish something the model would then try to call:

* a row is ``callable`` only when the tool was actually reloaded by this
  snapshot, is enabled, reachable, owned by a MyFlow user and carries a safe
  ``cf__<name>``; a row that stops qualifying is kept (the definition is data)
  but silently drops out of tool_search/tool_execute;
* ``content_hash`` always covers content + owner + call_name, so moving a tool
  between users re-embeds it even when the text is unchanged;
* ``schema_hash`` always covers the canonical input_schema, so a schema-only
  edit is visible to tool_execute without touching the vector index.

Every method opens exactly one short-lived session.  ``upsert_snapshot`` writes
the whole snapshot in a single transaction: the sync service commits the DB
first and only then rebuilds Milvus, so the database never lags the index.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from agentkit.contextforge.store.model import ContextForgeToolRow

#: Model/policy namespace. ContextForge MCP still receives the bare name.
CALL_NAME_PREFIX = "cf__"
#: ContextForge tool names are tool-call safe: [A-Za-z0-9_-]+, bounded length.
_MAX_NAME_CHARS = 64
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def call_name_for(name: str) -> str:
    """The model/policy name for a ContextForge tool name."""
    return CALL_NAME_PREFIX + name


def is_safe_tool_name(name: str) -> bool:
    """False for names the model could not call back safely."""
    return bool(name) and len(name) <= _MAX_NAME_CHARS and bool(_SAFE_NAME.fullmatch(name))


def content_hash_for(content: str, owner_user_id: str | None, call_name: str) -> str:
    """sha256(content + owner + call_name); all three drive the Milvus upsert."""
    payload = (content or "") + (owner_user_id or "") + (call_name or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_hash_for(input_schema: Any) -> str:
    """sha256 of the canonical JSON schema, independent of key order."""
    canonical = json.dumps(
        input_schema if input_schema is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextForgeToolRecord:
    """One tool row, detached from the session that produced it."""

    id: str
    name: str
    call_name: str
    display_name: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    parameters: tuple[str, ...] = ()
    owner_user_id: str | None = None
    enabled: bool = True
    reachable: bool = True
    callable: bool = False
    content: str = ""
    content_hash: str = ""
    schema_hash: str = ""
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ContextForgeSnapshotStats:
    """What one full-snapshot write changed."""

    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    disabled: int = 0
    #: Ids whose stored shape moved, in write order (the Milvus upsert set).
    changed_ids: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; the catalog contract is always UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record(row: ContextForgeToolRow) -> ContextForgeToolRecord:
    return ContextForgeToolRecord(
        id=row.id,
        name=row.name,
        call_name=row.call_name,
        display_name=row.display_name,
        description=row.description,
        input_schema=dict(row.input_schema or {}),
        annotations=dict(row.annotations or {}),
        tags=tuple(row.tags or ()),
        parameters=tuple(row.parameters or ()),
        owner_user_id=row.owner_user_id,
        enabled=bool(row.enabled),
        reachable=bool(row.reachable),
        callable=bool(row.callable),
        content=row.content,
        content_hash=row.content_hash,
        schema_hash=row.schema_hash,
        last_seen_at=_utc(row.last_seen_at),
    )


def _is_callable(
    record: ContextForgeToolRecord,
    *,
    name: str,
    call_name: str,
    seen_upstream: bool,
) -> bool:
    """The runtime gate. Everything that cannot be called safely fails here."""
    return bool(
        record.callable
        and seen_upstream
        and record.enabled
        and record.reachable
        and record.owner_user_id
        and is_safe_tool_name(name)
        and call_name == call_name_for(name)
    )


def _columns(
    record: ContextForgeToolRecord,
    *,
    name: str,
    call_name: str,
    callable_: bool,
    stamp: datetime,
) -> dict[str, Any]:
    """The stored shape of one record. Hashes are recomputed, never trusted."""
    return {
        "name": name,
        "call_name": call_name,
        "display_name": record.display_name,
        "description": record.description or "",
        "input_schema": dict(record.input_schema or {}),
        "annotations": dict(record.annotations or {}),
        "tags": list(record.tags or ()),
        "parameters": list(record.parameters or ()),
        "owner_user_id": record.owner_user_id,
        "enabled": bool(record.enabled),
        "reachable": bool(record.reachable),
        "callable": callable_,
        "content": record.content or "",
        "content_hash": content_hash_for(record.content, record.owner_user_id, call_name),
        "schema_hash": schema_hash_for(record.input_schema),
        "last_seen_at": _utc(record.last_seen_at) or stamp,
    }


#: Everything a snapshot rewrites; last_seen_at is excluded because every reload
#: stamps it and would otherwise mark the whole catalog as changed.
_COMPARED_COLUMNS = (
    "name",
    "call_name",
    "display_name",
    "description",
    "input_schema",
    "annotations",
    "tags",
    "parameters",
    "owner_user_id",
    "enabled",
    "reachable",
    "callable",
    "content",
    "content_hash",
    "schema_hash",
)


class ContextForgeToolStore:
    """Reads and writes for ``contextforge_tools``."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # -- writes ------------------------------------------------------------ #

    async def upsert_snapshot(
        self,
        records: Sequence[ContextForgeToolRecord],
        seen_names: Collection[str] | None = None,
    ) -> ContextForgeSnapshotStats:
        """Write a full ContextForge snapshot, then retire what it did not see.

        ``seen_names`` is the set of names this snapshot actually reloaded from
        ContextForge; ``None`` defaults to the record names, while an explicit
        empty collection means nothing was seen and nothing becomes callable.  A
        stored row outside it is no longer observed upstream, so it stops being
        callable but keeps its definition.  Records are matched by id first (a
        rename keeps the row) and by name second, so a tool that disappears and
        comes back reuses its row.
        """
        stamp = _now()
        incoming = [record for record in records if str(record.name or "").strip()]
        covered_names = {str(record.name).strip() for record in incoming}
        if seen_names is None:
            effective_seen = covered_names
        else:
            effective_seen = {str(name) for name in seen_names if name}
        inserted = updated = unchanged = disabled = 0
        changed_ids: list[str] = []

        async with self._sf() as session:
            rows = list((await session.scalars(select(ContextForgeToolRow))).all())
            by_id = {row.id: row for row in rows}
            by_name = {row.name: row for row in rows}

            for record in incoming:
                name = str(record.name).strip()
                call_name = str(record.call_name or "").strip() or call_name_for(name)
                values = _columns(
                    record,
                    name=name,
                    call_name=call_name,
                    callable_=_is_callable(
                        record,
                        name=name,
                        call_name=call_name,
                        seen_upstream=name in effective_seen,
                    ),
                    stamp=stamp,
                )
                row = by_id.get(record.id) if record.id else None
                if row is None:
                    row = by_name.get(name)
                if row is None:
                    row = ContextForgeToolRow(id=record.id or uuid4().hex, **values)
                    session.add(row)
                    by_id[row.id] = row
                    by_name[name] = row
                    inserted += 1
                    changed_ids.append(row.id)
                    continue
                changed = any(
                    getattr(row, column) != values[column] for column in _COMPARED_COLUMNS
                )
                for column, value in values.items():
                    setattr(row, column, value)
                if changed:
                    updated += 1
                    changed_ids.append(row.id)
                else:
                    unchanged += 1

            for row in rows:
                if row.name in covered_names or not row.callable:
                    continue
                row.callable = False
                disabled += 1
                changed_ids.append(row.id)

            await session.commit()

        return ContextForgeSnapshotStats(
            seen=len(incoming),
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            disabled=disabled,
            changed_ids=tuple(changed_ids),
        )

    # -- reads ------------------------------------------------------------- #

    async def get_by_ids(
        self, ids: Sequence[str], owner_user_id: str | None
    ) -> list[ContextForgeToolRecord]:
        """Callable tools for the given ids, in the order given (Milvus rank)."""
        wanted: list[str] = []
        for item in ids:
            if item and str(item) not in wanted:
                wanted.append(str(item))
        if not wanted:
            return []
        stmt = select(ContextForgeToolRow).where(
            ContextForgeToolRow.id.in_(wanted),
            ContextForgeToolRow.callable.is_(True),
        )
        if owner_user_id is not None:
            stmt = stmt.where(ContextForgeToolRow.owner_user_id == owner_user_id)
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        found = {row.id: _record(row) for row in rows}
        return [found[tool_id] for tool_id in wanted if tool_id in found]

    async def get_by_call_name(
        self, call_name: str, owner_user_id: str | None
    ) -> ContextForgeToolRecord | None:
        """The runtime lookup tool_execute performs: owner + callable + call_name."""
        if not call_name:
            return None
        stmt = select(ContextForgeToolRow).where(
            ContextForgeToolRow.call_name == call_name,
            ContextForgeToolRow.callable.is_(True),
        )
        if owner_user_id is not None:
            stmt = stmt.where(ContextForgeToolRow.owner_user_id == owner_user_id)
        async with self._sf() as session:
            row = (await session.scalars(stmt)).first()
        return _record(row) if row is not None else None

    async def list_callable(self, owner_user_id: str | None) -> list[ContextForgeToolRecord]:
        """Every tool the runtime may offer, optionally scoped to one user."""
        return await self._list(owner_user_id, callable_only=True)

    async def list_all(self, owner_user_id: str | None) -> list[ContextForgeToolRecord]:
        """Every stored tool, including the ones the runtime refuses."""
        return await self._list(owner_user_id, callable_only=False)

    async def _list(
        self, owner_user_id: str | None, *, callable_only: bool
    ) -> list[ContextForgeToolRecord]:
        stmt = select(ContextForgeToolRow)
        if callable_only:
            stmt = stmt.where(ContextForgeToolRow.callable.is_(True))
        if owner_user_id is not None:
            stmt = stmt.where(ContextForgeToolRow.owner_user_id == owner_user_id)
        stmt = stmt.order_by(
            ContextForgeToolRow.call_name if callable_only else ContextForgeToolRow.name,
            ContextForgeToolRow.id,
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        return [_record(row) for row in rows]


__all__ = [
    "CALL_NAME_PREFIX",
    "ContextForgeSnapshotStats",
    "ContextForgeToolRecord",
    "ContextForgeToolStore",
    "call_name_for",
    "content_hash_for",
    "is_safe_tool_name",
    "schema_hash_for",
]
