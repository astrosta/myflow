"""SQL store for `mcp_servers`.

The store owns the invariants the rest of the runtime relies on:

* **a server is user-private.**  Every read and write takes the owner and filters on
  it, so there is no way to reach another user's row by guessing an id or a name.
  `list_all()` is the deliberate admin escape hatch (the nightly sync and the global
  status page need every owner);
* **`name` and `owner_user_id` never change.**  A server's `name` is baked into every
  derived `mcp__<server>__<tool>` call name, so `update()` refuses them instead of
  letting a rename invalidate tools the model already retrieved;
* **each lifecycle concern has exactly one writer.**  `update()` writes the editable
  configuration (display name, description, url, transport, headers, TLS),
  `set_enabled()` writes the enable switch *and* the status it implies, and
  `set_status()` writes the sync stamps.  Nothing else may set those columns;
* **`last_error` is bounded** at the storage boundary, because the upstream text is
  not ours to trust.

Every method opens exactly one short-lived session and commits before returning, so
callers always hold a detached record.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from agentkit.mcp.models import (
    McpServerRecord,
    truncate_error,
    validate_server_name,
    validate_server_status,
    validate_transport,
)
from agentkit.mcp.store.model import McpServerRow

#: The columns a user edits through PUT /api/mcp-servers/{id}.  Everything else is
#: either immutable (id/name/owner) or written by set_enabled()/set_status().
_MUTABLE_COLUMNS = (
    "display_name",
    "description",
    "transport",
    "url",
    "headers_encrypted",
    "verify_tls",
)
_IMMUTABLE_COLUMNS = ("id", "name", "owner_user_id")


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; the store's contract is always UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_owner(owner_user_id: str | None) -> str:
    """Every scoped access needs an owner; `None` is a programming error."""
    owner = str(owner_user_id).strip() if owner_user_id else ""
    if not owner:
        raise ValueError(
            "owner_user_id is required: MCP servers are user-private "
            "(use list_all for the admin view)"
        )
    return owner


def _record(row: McpServerRow) -> McpServerRecord:
    return McpServerRecord(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description or "",
        transport=row.transport,
        url=row.url,
        headers_encrypted=row.headers_encrypted,
        verify_tls=bool(row.verify_tls),
        enabled=bool(row.enabled),
        status=row.status,
        reachable=bool(row.reachable),
        last_error=row.last_error,
        last_connected_at=_utc(row.last_connected_at),
        last_tools_sync_at=_utc(row.last_tools_sync_at),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


class McpServerStore:
    """Reads and writes for `mcp_servers`."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # -- writes ------------------------------------------------------------ #

    async def create(self, record: McpServerRecord) -> McpServerRecord:
        """Insert one server and return the stored row.

        A duplicate `(owner_user_id, name)` raises `IntegrityError`: the API turns it
        into a 409, and swallowing it here would hand the caller a row that does not
        exist.
        """
        owner = _require_owner(record.owner_user_id)
        name = validate_server_name(record.name)
        transport = validate_transport(record.transport)
        enabled = bool(record.enabled)
        status = validate_server_status(record.status)
        if not enabled:
            # A server the user switched off is "disabled" no matter what it was.
            status = "disabled"
        stamp = _now()
        row = McpServerRow(
            id=(record.id or "").strip() or uuid4().hex,
            owner_user_id=owner,
            name=name,
            display_name=record.display_name,
            description=str(record.description or ""),
            transport=transport,
            url=str(record.url or "").strip(),
            headers_encrypted=record.headers_encrypted or None,
            verify_tls=bool(record.verify_tls),
            enabled=enabled,
            status=status,
            reachable=bool(record.reachable),
            last_error=truncate_error(record.last_error),
            last_connected_at=_utc(record.last_connected_at),
            last_tools_sync_at=_utc(record.last_tools_sync_at),
            created_at=_utc(record.created_at) or stamp,
            updated_at=_utc(record.updated_at) or stamp,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _record(row)

    async def update(
        self, server_id: str, owner_user_id: str, fields: Mapping[str, Any]
    ) -> McpServerRecord | None:
        """Write the user-editable columns; `None` when this owner has no such server.

        `enabled` and `status` are rejected on purpose: they are derived state and
        have their own writers (`set_enabled` / `set_status`), so an edit form cannot
        mark a server active without a sync behind it.
        """
        _require_owner(owner_user_id)
        values = _update_values(fields)
        return await self._write(server_id, owner_user_id, values)

    async def set_enabled(
        self, server_id: str, owner_user_id: str, enabled: bool
    ) -> McpServerRecord | None:
        """Switch a server on or off, with the status that follows from it.

        Disabling makes the status `disabled`; re-enabling makes it `pending`,
        because the tools this server offers are not trustworthy again until the next
        successful `initialize` + `tools/list`.
        """
        switch = bool(enabled)
        values = {"enabled": switch, "status": "pending" if switch else "disabled"}
        return await self._write(server_id, owner_user_id, values)

    async def set_status(
        self,
        server_id: str,
        owner_user_id: str,
        *,
        status: str,
        reachable: bool,
        last_error: str | None = None,
        last_connected_at: datetime | None = None,
        last_tools_sync_at: datetime | None = None,
    ) -> McpServerRecord | None:
        """Record the result of one sync/connect attempt.

        `last_error` is always written: pass the failure text, or `None` to clear it
        (which is what a successful connect does).  The two timestamps are written
        only when supplied, so a failure cannot erase the last success.
        """
        values: dict[str, Any] = {
            "status": validate_server_status(status),
            "reachable": bool(reachable),
            "last_error": truncate_error(last_error),
        }
        if last_connected_at is not None:
            values["last_connected_at"] = _utc(last_connected_at)
        if last_tools_sync_at is not None:
            values["last_tools_sync_at"] = _utc(last_tools_sync_at)
        return await self._write(server_id, owner_user_id, values)

    async def delete(self, server_id: str, owner_user_id: str) -> bool:
        """Delete one server; False when it is not this owner's."""
        owner = _require_owner(owner_user_id)
        async with self._sf() as session:
            row = (
                await session.scalars(
                    select(McpServerRow).where(
                        McpServerRow.id == server_id,
                        McpServerRow.owner_user_id == owner,
                    )
                )
            ).first()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    async def _write(
        self, server_id: str, owner_user_id: str, values: Mapping[str, Any]
    ) -> McpServerRecord | None:
        owner = _require_owner(owner_user_id)
        async with self._sf() as session:
            row = (
                await session.scalars(
                    select(McpServerRow).where(
                        McpServerRow.id == server_id,
                        McpServerRow.owner_user_id == owner,
                    )
                )
            ).first()
            if row is None:
                return None
            for column, value in values.items():
                setattr(row, column, value)
            row.updated_at = _now()
            await session.commit()
            await session.refresh(row)
            return _record(row)

    # -- reads ------------------------------------------------------------- #

    async def get(self, server_id: str, owner_user_id: str) -> McpServerRecord | None:
        """One server, only if this owner created it."""
        owner = _require_owner(owner_user_id)
        async with self._sf() as session:
            row = (
                await session.scalars(
                    select(McpServerRow).where(
                        McpServerRow.id == server_id,
                        McpServerRow.owner_user_id == owner,
                    )
                )
            ).first()
        return _record(row) if row is not None else None

    async def get_by_name(self, owner_user_id: str, name: str) -> McpServerRecord | None:
        """One server by its slug, only if this owner created it."""
        owner = _require_owner(owner_user_id)
        if not name:
            return None
        async with self._sf() as session:
            row = (
                await session.scalars(
                    select(McpServerRow).where(
                        McpServerRow.owner_user_id == owner,
                        McpServerRow.name == name,
                    )
                )
            ).first()
        return _record(row) if row is not None else None

    async def list_for_owner(self, owner_user_id: str) -> list[McpServerRecord]:
        """Every server this owner owns, by slug (the management list order)."""
        owner = _require_owner(owner_user_id)
        stmt = (
            select(McpServerRow)
            .where(McpServerRow.owner_user_id == owner)
            .order_by(McpServerRow.name, McpServerRow.id)
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        return [_record(row) for row in rows]

    async def list_all(self) -> list[McpServerRecord]:
        """Every stored server, every owner: the nightly sync and status page view."""
        stmt = select(McpServerRow).order_by(
            McpServerRow.owner_user_id, McpServerRow.name, McpServerRow.id
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        return [_record(row) for row in rows]


def _update_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an `update()` payload before the row is touched."""
    if not isinstance(fields, Mapping):
        raise ValueError("mcp_servers update fields must be a mapping")
    for key in fields:
        if key in _IMMUTABLE_COLUMNS:
            raise ValueError(
                f"mcp_servers.{key} is not an updatable column: name and owner_user_id "
                "are immutable, and id belongs to the store"
            )
        if key not in _MUTABLE_COLUMNS:
            raise ValueError(
                f"mcp_servers has no updatable column {key!r}: editable columns are "
                f"{list(_MUTABLE_COLUMNS)}; use set_enabled/set_status for state"
            )
    values: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "transport":
            values[key] = validate_transport(value)
        elif key == "url":
            values[key] = str(value or "").strip()
        elif key == "description":
            values[key] = str(value or "")
        elif key == "headers_encrypted":
            values[key] = value or None
        elif key == "verify_tls":
            values[key] = bool(value)
        else:
            values[key] = value
    return values


__all__ = ["McpServerStore"]
