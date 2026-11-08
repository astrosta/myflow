"""SQL store for `mcp_tools`, the per-server tool snapshot the runtime reads.

The store owns the invariants the rest of the runtime relies on, so a sync service
cannot accidentally publish something the model would then try to call:

* **identity is derived, never taken from the caller.**
  `id = uuid5(NAMESPACE_URL, "<server_id>:<name>")` and
  `call_name = "mcp__<server_name>__<name>"`.  Both are pure functions of the row's
  own inputs, so a re-sync of the same server always rewrites the same Milvus pk and
  the same call name; a record's incoming `id`/`content_hash`/`schema_hash` are
  output fields and are ignored on write;
* **a row is `callable` only when the snapshot actually saw it**, the tool is
  enabled, its server is reachable, it has an owner and it carries a safe
  `mcp__<server>__<tool>`.  Callability is derived by the store, never copied from
  `record.callable`, so a sync service cannot promote a tool the server state
  forbids.  A row that stops qualifying is kept (the definition is data) but drops
  out of tool_search/tool_execute;
* **`last_seen_at` only moves for a tool this snapshot saw**; a row carried over
  for an unreachable or disabled server keeps the timestamp of its last real
  sighting;
* **`content_hash` covers content + owner + call_name**, so moving a tool between
  servers or owners re-embeds it even when the text is unchanged, and
  **`schema_hash` covers the canonical input_schema**, so a schema-only edit is
  visible to tool_execute without touching the vector index;
* **tools are owner-private and never shared.**  Every scoped read takes the owner
  and filters on it, so no caller can reach another user's tool by guessing an id or
  a call name.  `list_all(None)` is the deliberate admin/index escape hatch.
  The writer trusts the `(server_id, owner_user_id)` pair it is handed - it does not
  re-read `mcp_servers` - because inheriting the owner from the server row is the
  sync's job; re-checking here would couple the two tables for every snapshot.

Every method opens exactly one short-lived session.  `upsert_snapshot` writes the
whole snapshot in a single transaction: the sync commits the database first and only
then rebuilds Milvus, so the index never publishes a row the database does not have.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from agentkit.mcp.models import MAX_SERVER_NAME_CHARS
from agentkit.mcp.store.model import McpToolRow

#: Model/policy namespace; the upstream server still receives the bare `name`.
CALL_NAME_PREFIX = "mcp__"
_CALL_NAME_SEPARATOR = "__"
#: Every provider's function-name budget is at most this long, so a longer call name
#: could not be handed to the model even if the upstream tool existed.
MAX_CALL_NAME_CHARS = 128
#: MCP tool names are bounded as well; the value only needs to bound the regex.
MAX_TOOL_NAME_CHARS = 128
#: Column bounds. PostgreSQL enforces them and SQLite does not, so the store applies
#: them itself instead of letting one backend accept a row the other rejects.
MAX_STORED_NAME_CHARS = 255
MAX_DISPLAY_NAME_CHARS = 512

#: A server slug cannot contain "_", which is what makes the call name unambiguous.
_SAFE_SERVER_NAME = re.compile(rf"^[a-z0-9-]{{1,{MAX_SERVER_NAME_CHARS}}}$")
#: A tool name the model can call back: the character set every provider accepts in
#: a function name.  A dot or a space is refused (see `is_safe_call_name`).
_SAFE_CALL_NAME = re.compile(
    rf"^{CALL_NAME_PREFIX}[a-z0-9-]{{1,{MAX_SERVER_NAME_CHARS}}}"
    rf"{_CALL_NAME_SEPARATOR}[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_TOOL_NAME_CHARS - 1}}}$"
)


def call_name_for(server_name: str, tool_name: str) -> str:
    """The model/policy name for one tool of one server.

    Deliberately a plain concatenation: a server slug or a tool name that does not
    belong in a call name produces an unsafe name, which `is_safe_call_name` rejects
    and the store refuses to publish, instead of being silently rewritten into
    something the model would call.
    """
    return (
        CALL_NAME_PREFIX
        + str(server_name or "")
        + _CALL_NAME_SEPARATOR
        + str(tool_name or "")
    )


def tool_id_for(server_id: str, tool_name: str) -> str:
    """The stable row id (and Milvus pk) of one tool of one server."""
    return str(uuid5(NAMESPACE_URL, str(server_id) + ":" + str(tool_name)))


def is_safe_call_name(name: str) -> bool:
    """False for a call name the model could not call back safely.

    The whole name must be `mcp__<server slug>__<tool>`, bounded to a length every
    provider accepts.  The tool part is limited to `[A-Za-z0-9_-]`: MCP also allows
    a dot, but no provider accepts one in a function name, so a dotted upstream tool
    is stored and stays non-callable rather than poisoning a model request.
    """
    text = str(name or "")
    return (
        bool(text)
        and len(text) <= MAX_CALL_NAME_CHARS
        and bool(_SAFE_CALL_NAME.fullmatch(text))
    )


def content_hash_for(
    content: str, owner_user_id: str | None, call_name: str
) -> str:
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


def _server_name_from_call_name(call_name: str) -> str:
    """Recover the server slug from a call name, or "" when it is not one.

    A slug is `[a-z0-9-]+` and therefore cannot contain the "__" separator, so the
    first segment after the prefix is always the server name even when the upstream
    tool name contains "__" itself.
    """
    text = str(call_name or "")
    if not text.startswith(CALL_NAME_PREFIX):
        return ""
    server_name, separator, tool_name = text[len(CALL_NAME_PREFIX) :].partition(
        _CALL_NAME_SEPARATOR
    )
    if not separator or not tool_name:
        return ""
    return server_name if _SAFE_SERVER_NAME.fullmatch(server_name) else ""


@dataclass(frozen=True, slots=True)
class McpToolRecord:
    """One `mcp_tools` row, detached from the session that produced it.

    A record is both the stored shape and the input shape of `upsert_snapshot`.
    `id`, `call_name` (when omitted), `content_hash`, `schema_hash` and `callable`
    are outputs: the store derives them from the record's own fields, so a writer
    cannot set them, and a reader gets what was actually stored.
    """

    id: str = ""
    server_id: str = ""
    #: The owning server's slug.  Optional on write: it is then recovered from
    #: `call_name`, which is what the sync already has.
    server_name: str = ""
    owner_user_id: str = ""
    name: str = ""
    call_name: str = ""
    display_name: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    parameters: tuple[str, ...] = ()
    enabled: bool = True
    reachable: bool = True
    callable: bool = False
    content: str = ""
    content_hash: str = ""
    schema_hash: str = ""
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class McpToolSnapshotStats:
    """What one full-snapshot write changed.

    `inserted + updated + unchanged` is the number of records the snapshot wrote;
    `disabled` counts the rows that stopped being callable (whether the snapshot
    resupplied them or not).
    """

    #: Tool ids this snapshot observed upstream (the size of `seen_ids`).
    seen: int = 0
    inserted: int = 0
    #: Existing rows whose definition changed.
    updated: int = 0
    #: Existing rows whose definition is unchanged.
    unchanged: int = 0
    #: Rows that stopped being callable.
    disabled: int = 0
    #: Callable ids that are new or changed, in write order: the Milvus upsert set.
    upsert_ids: tuple[str, ...] = ()
    #: Rows that stopped being callable, in write order: the Milvus delete set.
    delete_ids: tuple[str, ...] = ()


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
    """Runtime reads are always owner-scoped; `None` is a programming error."""
    owner = str(owner_user_id).strip() if owner_user_id else ""
    if not owner:
        raise ValueError(
            "owner_user_id is required: MCP tools are user-private "
            "(use list_all(None) for the admin view)"
        )
    return owner


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One record with the identity fields the store derives, resolved and validated."""

    record: McpToolRecord
    id: str
    server_id: str
    server_name: str
    owner_user_id: str
    name: str
    call_name: str


def _prepare(record: McpToolRecord) -> _Prepared | None:
    """Resolve one record's identity, or refuse it.

    A tool without a name - or with one longer than the column can hold, which MCP
    itself caps at 128 characters - is not a tool anything could call back: it is
    dropped silently, like the retired catalog dropped nameless tools.  A tool
    without a server or an owner cannot be stored at all (`server_id` and
    `owner_user_id` are NOT NULL), so that is a caller bug and fails loudly instead
    of writing a row nobody can reach.
    """
    name = str(record.name or "").strip()
    if not name or len(name) > MAX_STORED_NAME_CHARS:
        return None
    server_id = str(record.server_id or "").strip()
    if not server_id:
        raise ValueError("mcp_tools.server_id is required: a tool belongs to one server")
    owner = str(record.owner_user_id or "").strip()
    if not owner:
        raise ValueError(
            "mcp_tools.owner_user_id is required: tools inherit the server's owner "
            "and are never shared"
        )
    call_name = str(record.call_name or "").strip()
    server_name = str(record.server_name or "").strip() or _server_name_from_call_name(
        call_name
    )
    if not call_name:
        if not server_name:
            raise ValueError(
                "mcp_tools.call_name is required: pass server_name or the call_name "
                "the tool is published under"
            )
        call_name = call_name_for(server_name, name)
    return _Prepared(
        record=record,
        id=tool_id_for(server_id, name),
        server_id=server_id,
        server_name=server_name,
        owner_user_id=owner,
        name=name,
        call_name=call_name,
    )


def _is_callable(prepared: _Prepared, *, seen_upstream: bool) -> bool:
    """The runtime gate. Everything that cannot be called safely fails here.

    Callability is derived from the snapshot and the record's fields; the
    caller-supplied `record.callable` is an output flag, not an input, so a sync
    service cannot promote a tool the server state forbids.
    """
    record = prepared.record
    return bool(
        seen_upstream
        and record.enabled
        and record.reachable
        # `_prepare` already refuses an ownerless record; the clause is kept because
        # "owner present" is part of the published rule, not an implementation detail.
        and prepared.owner_user_id
        and is_safe_call_name(prepared.call_name)
        and prepared.call_name == call_name_for(prepared.server_name, prepared.name)
    )


def _display_name(value: str | None) -> str | None:
    """A display label is cosmetic, so a runaway title is truncated, not rejected."""
    if value is None:
        return None
    return str(value)[:MAX_DISPLAY_NAME_CHARS]


def _columns(
    prepared: _Prepared,
    *,
    callable_: bool,
    last_seen_at: datetime,
) -> dict[str, Any]:
    """The stored shape of one record. Hashes are recomputed, never trusted."""
    record = prepared.record
    return {
        "server_id": prepared.server_id,
        "server_name": prepared.server_name,
        "owner_user_id": prepared.owner_user_id,
        "name": prepared.name,
        "call_name": prepared.call_name,
        "display_name": _display_name(record.display_name),
        "description": record.description or "",
        "input_schema": dict(record.input_schema or {}),
        "annotations": dict(record.annotations or {}),
        "parameters": list(record.parameters or ()),
        "content": record.content or "",
        "content_hash": content_hash_for(
            record.content, prepared.owner_user_id, prepared.call_name
        ),
        "schema_hash": schema_hash_for(record.input_schema),
        "enabled": bool(record.enabled),
        "reachable": bool(record.reachable),
        "callable": callable_,
        "last_seen_at": last_seen_at,
    }


#: Everything a snapshot rewrites; last_seen_at is excluded because a sighting stamp
#: is not a definition change and would otherwise mark the whole catalog as changed.
_COMPARED_COLUMNS = (
    "server_id",
    "server_name",
    "owner_user_id",
    "name",
    "call_name",
    "display_name",
    "description",
    "input_schema",
    "annotations",
    "parameters",
    "content",
    "content_hash",
    "schema_hash",
    "enabled",
    "reachable",
    "callable",
)


def _last_seen_at(
    prepared: _Prepared,
    row: McpToolRow | None,
    *,
    seen_upstream: bool,
    stamp: datetime,
) -> datetime:
    """The last real appearance of the tool in that server's tools/list.

    A record this snapshot saw was just observed, so its timestamp moves.  A record
    the snapshot carried without seeing it (an unreachable or disabled server) is
    persisted without pretending it appeared upstream: an existing row keeps the
    timestamp of its last real sighting, and a new row is stamped on first
    discovery only.
    """
    record = prepared.record
    if seen_upstream:
        return _utc(record.last_seen_at) or stamp
    if row is not None and row.last_seen_at is not None:
        return _utc(row.last_seen_at) or stamp
    return _utc(record.last_seen_at) or stamp


def _record(row: McpToolRow) -> McpToolRecord:
    return McpToolRecord(
        id=row.id,
        server_id=row.server_id,
        server_name=row.server_name,
        owner_user_id=row.owner_user_id,
        name=row.name,
        call_name=row.call_name,
        display_name=row.display_name,
        description=row.description or "",
        input_schema=dict(row.input_schema or {}),
        annotations=dict(row.annotations or {}),
        parameters=tuple(row.parameters or ()),
        enabled=bool(row.enabled),
        reachable=bool(row.reachable),
        callable=bool(row.callable),
        content=row.content or "",
        content_hash=row.content_hash,
        schema_hash=row.schema_hash,
        last_seen_at=_utc(row.last_seen_at),
    )


class McpToolStore:
    """Reads and writes for `mcp_tools`."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # -- writes ------------------------------------------------------------ #

    async def upsert_snapshot(
        self,
        records: Sequence[McpToolRecord],
        seen_ids: Collection[str],
    ) -> McpToolSnapshotStats:
        """Write a full MCP tool snapshot and retire what it did not rewrite.

        `seen_ids` is required: it is the set of tool ids this snapshot actually
        reloaded from a server's tools/list, and callability is derived from it.  It
        may be empty (nothing was seen, so nothing becomes callable) but it may not be
        omitted or `None`; a caller that forgot it would otherwise publish tools
        nobody observed.

        The snapshot is **full**: every stored row this write did not rewrite stops
        being callable, including a tool that vanished from tools/list entirely (it
        is in neither `records` nor `seen_ids`).  Callers must therefore pass every
        server they are responsible for; a per-server snapshot would retire the
        other servers' tools.  Rows are matched by `(server_id, name)`, which is also
        what `tool_id_for` derives the id from, so a re-sync reuses its row; if one
        snapshot carries the same tool twice, the last record wins.
        """
        if seen_ids is None:
            raise ValueError(
                "seen_ids is required: callability depends on what the snapshot saw"
            )
        stamp = _now()
        observed = {str(tool_id) for tool_id in seen_ids if tool_id}
        prepared = [item for item in map(_prepare, records) if item is not None]
        inserted = updated = unchanged = disabled = 0
        upsert_ids: list[str] = []
        delete_ids: list[str] = []

        async with self._sf() as session:
            rows = list((await session.scalars(select(McpToolRow))).all())
            by_key = {(row.server_id, row.name): row for row in rows}
            rewritten: set[str] = set()

            for item in prepared:
                seen_upstream = item.id in observed
                callable_ = _is_callable(item, seen_upstream=seen_upstream)
                row = by_key.get((item.server_id, item.name))
                values = _columns(
                    item,
                    callable_=callable_,
                    last_seen_at=_last_seen_at(
                        item,
                        row,
                        seen_upstream=seen_upstream,
                        stamp=stamp,
                    ),
                )
                if row is None:
                    row = McpToolRow(id=item.id, **values)
                    session.add(row)
                    by_key[(item.server_id, item.name)] = row
                    rewritten.add(row.id)
                    inserted += 1
                    if callable_:
                        upsert_ids.append(row.id)
                    continue
                # Track the id the row actually has, not the one we just derived, so a
                # legacy row can never be rewritten and retired in the same snapshot.
                rewritten.add(row.id)
                was_callable = bool(row.callable)
                changed = any(
                    getattr(row, column) != values[column] for column in _COMPARED_COLUMNS
                )
                for column, value in values.items():
                    setattr(row, column, value)
                if changed:
                    updated += 1
                    if callable_:
                        upsert_ids.append(row.id)
                else:
                    unchanged += 1
                if was_callable and not callable_:
                    disabled += 1
                    delete_ids.append(row.id)

            # A row this snapshot did not rewrite was not revalidated, so it cannot
            # stay callable: the definition is kept, runtime availability is not.
            for row in rows:
                if row.id in rewritten or not row.callable:
                    continue
                row.callable = False
                disabled += 1
                delete_ids.append(row.id)

            await session.commit()

        return McpToolSnapshotStats(
            seen=len(observed),
            inserted=inserted,
            updated=updated,
            unchanged=unchanged,
            disabled=disabled,
            upsert_ids=tuple(upsert_ids),
            delete_ids=tuple(delete_ids),
        )

    async def delete_by_server(self, server_id: str) -> list[str]:
        """Physically delete every tool of one server and return their ids.

        Deleting a server deletes its tools (spec 4.2 rule 3).  Every id comes back,
        callable or not, so the caller can hand the whole list to the vector index:
        deleting an id it does not have is a no-op, while a missed id would leave a
        stale vector behind.  The caller has already authenticated the owner, so this
        deletes by server id alone.
        """
        target = str(server_id or "").strip()
        if not target:
            return []
        async with self._sf() as session:
            ids = list(
                (
                    await session.scalars(
                        select(McpToolRow.id)
                        .where(McpToolRow.server_id == target)
                        .order_by(McpToolRow.name, McpToolRow.id)
                    )
                ).all()
            )
            if not ids:
                return []
            await session.execute(
                sa_delete(McpToolRow).where(McpToolRow.server_id == target)
            )
            await session.commit()
        return [str(tool_id) for tool_id in ids]

    # -- reads ------------------------------------------------------------- #

    async def get_by_call_name(
        self, call_name: str, owner_user_id: str
    ) -> McpToolRecord | None:
        """The runtime lookup tool_execute performs, owner-scoped.

        Returns the stored definition even when it is no longer callable: the caller
        needs `record.callable` (and the server's state) to explain *why* it refuses
        the call, which a filtered-out `None` could not.  The owner filter is part of
        the query, so a guessed call name never reaches another user's row.
        """
        owner = _require_owner(owner_user_id)
        if not call_name:
            return None
        stmt = select(McpToolRow).where(
            McpToolRow.call_name == call_name,
            McpToolRow.owner_user_id == owner,
        )
        async with self._sf() as session:
            row = (await session.scalars(stmt)).first()
        return _record(row) if row is not None else None

    async def get_by_ids(
        self, ids: Sequence[str], owner_user_id: str
    ) -> list[McpToolRecord]:
        """Callable tools for the given ids, in the order given (Milvus rank).

        This is the database join behind tool_search, so it filters on `callable` as
        well as the owner: a row that dropped out of the runtime must not come back
        through a stale vector id.
        """
        owner = _require_owner(owner_user_id)
        wanted: list[str] = []
        for item in ids:
            if item and str(item) not in wanted:
                wanted.append(str(item))
        if not wanted:
            return []
        stmt = select(McpToolRow).where(
            McpToolRow.id.in_(wanted),
            McpToolRow.callable.is_(True),
            McpToolRow.owner_user_id == owner,
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        found = {row.id: _record(row) for row in rows}
        return [found[tool_id] for tool_id in wanted if tool_id in found]

    async def list_all(self, owner_user_id: str | None = None) -> list[McpToolRecord]:
        """Every stored tool, including the ones the runtime refuses.

        `None` deliberately lists every owner: this is the admin/index surface (the
        nightly sync and the full Milvus rebuild).  Pass an owner for the
        owner-scoped view; a blank owner is refused rather than silently
        half-scoped, because "" is never a user id.
        """
        stmt = select(McpToolRow)
        if owner_user_id is not None:
            stmt = stmt.where(McpToolRow.owner_user_id == _require_owner(owner_user_id))
        stmt = stmt.order_by(
            McpToolRow.owner_user_id,
            McpToolRow.server_name,
            McpToolRow.name,
            McpToolRow.id,
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        return [_record(row) for row in rows]

    async def list_for_server(
        self, server_id: str, owner_user_id: str
    ) -> list[McpToolRecord]:
        """Every tool of one server, callable or not: the management list view.

        The owner is required as well as the server id, so a stale id from another
        user's session cannot enumerate someone else's tools.
        """
        owner = _require_owner(owner_user_id)
        target = str(server_id or "").strip()
        if not target:
            return []
        stmt = (
            select(McpToolRow)
            .where(
                McpToolRow.server_id == target,
                McpToolRow.owner_user_id == owner,
            )
            .order_by(McpToolRow.name, McpToolRow.id)
        )
        async with self._sf() as session:
            rows = (await session.scalars(stmt)).all()
        return [_record(row) for row in rows]


__all__ = [
    "CALL_NAME_PREFIX",
    "MAX_CALL_NAME_CHARS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_STORED_NAME_CHARS",
    "MAX_TOOL_NAME_CHARS",
    "McpToolRecord",
    "McpToolSnapshotStats",
    "McpToolStore",
    "call_name_for",
    "content_hash_for",
    "is_safe_call_name",
    "schema_hash_for",
    "tool_id_for",
]
