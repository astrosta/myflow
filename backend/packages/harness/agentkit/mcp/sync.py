"""Discovery sync: read every server once, write one full mcp_tools snapshot.

The tool store is a **full-catalog** writer: any row a snapshot does not rewrite loses
callability.  That single property decides the shape of this module:

* **one snapshot per sync, over every server of every owner.** sync() reads
  mcp_servers with list_all(), reads the stored tools once, and calls
  upsert_snapshot(records, seen_ids) exactly once - with zero servers included as an
  empty snapshot, never with one server's records.  A per-server snapshot would
  retire the other servers' tools;
* **a server failure is per server.** A server whose tools/list fails (or whose
  headers cannot be read) contributes its *previous* rows with reachable=False and is
  kept out of seen_ids: the definitions stay in the database and drop out of
  tool_search/tool_execute, while every other server still syncs and is published;
* **a disabled server is never contacted.** Its rows are carried the same way, with
  enabled=False, so switching a server off does not lose what it offered;
* **tools inherit the server's owner.** A stored row with the same (server_id, name)
  but a different owner is never rewritten - the store matches rows by that pair and
  would hand somebody else's tool to this owner - so it is skipped and reported in
  McpSyncStats.conflicts;
* **the database commits before the index.** The vector index only ever reflects a
  committed catalog; an index failure sets index_failed and leaves the commit.

Secrets: the sync decrypts a server's headers to build the connection (the client
never sees a cipher), and every failure note it stores is reduced to the server slug
plus an exception class - a credential, a bearer token or a URL with a query string
must never reach last_error, a log line or the API.

What this module deliberately does not do: it does not write mcp_servers.status (the
refresh that owns the run stamps that, from this sync's stats), it does not build the
vector index itself (Task 6 owns the index), and it does not schedule anything.  Its
dependencies are protocols, so a test can substitute any of them.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from mcp import types as mcp_types

from agentkit.mcp.catalog import DEFAULT_MAX_DOCUMENT_CHARS, build_tool_document
from agentkit.mcp.client import McpServerConnection
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.errors import McpServerConfigurationError, McpServerError
from agentkit.mcp.models import McpServerRecord, truncate_error
from agentkit.mcp.store.tool_repository import (
    McpToolRecord,
    call_name_for,
    content_hash_for,
    schema_hash_for,
    tool_id_for,
)

logger = logging.getLogger(__name__)


class McpServerSource(Protocol):
    """The server rows a sync walks: every owner, the admin view.

    Read-only on purpose.  A sync discovers tools; the mcp_servers status stamps
    (active / error / reachable / last_error) belong to the refresh that orchestrated
    it, which is the only layer that knows whether the whole run succeeded.
    """

    async def list_all(self) -> Sequence[McpServerRecord]: ...


class McpToolSink(Protocol):
    """The tool store: the full-snapshot writer and the catalogue reader."""

    async def list_all(
        self, owner_user_id: str | None = None
    ) -> Sequence[McpToolRecord]: ...

    async def upsert_snapshot(
        self, records: Sequence[McpToolRecord], seen_ids: Collection[str]
    ) -> Any: ...


class McpToolLister(Protocol):
    """The client: one full, paginated tools/list per server."""

    async def list_tools(
        self, connection: McpServerConnection
    ) -> Sequence[mcp_types.Tool]: ...


class McpToolIndex(Protocol):
    """The vector index; the sync only asks it to catch up with the commit."""

    async def sync(
        self,
        records: Sequence[McpToolRecord],
        *,
        embedder: Any = None,
        full: bool = False,
    ) -> Any: ...


class McpConnectionFactory(Protocol):
    """Builds the connection for one server; the injection point for decryption."""

    def __call__(self, server: McpServerRecord) -> McpServerConnection: ...


@dataclass(frozen=True, slots=True)
class McpSyncStats:
    """What one discovery sync did.

    scanned counts the tool rows the snapshot carried (discovered plus carried),
    servers_scanned the servers walked (enabled or not), and changed the rows whose
    definition actually changed (inserted + updated); upserted/deleted are the
    vector-index work lists the store reported.
    """

    scanned: int = 0
    servers_scanned: int = 0
    servers_failed: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    #: Sanitized notes for the failed servers and the index, joined with "; ".
    error: str | None = None
    index_failed: bool = False
    duration_ms: int = 0
    #: Call names skipped because their stored row belongs to another user.
    conflicts: tuple[str, ...] = ()


#: These run over failure text that is stored and shown in the UI, so they scrub
#: anything that could carry a credential or an endpoint the user never published.
_URL = re.compile(r"(?i)[a-z][a-z0-9+.\-]*://[^\s]+")
_CREDENTIAL_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
    r"api-key|x-auth-token|x-access-token)\b\s*[:=][^\r\n]*"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key)\s*[:=]\s*\S+"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _server_label(server: McpServerRecord) -> str:
    """A stable, non-secret way to name one server in a message."""
    name = str(getattr(server, "name", "") or "").strip()
    if name:
        return "MCP server {0!r}".format(name)
    return "MCP server {0!r}".format(getattr(server, "id", ""))


def sanitize_sync_error(text: str | None) -> str:
    """Scrub an upstream message of endpoints, credentials and control characters."""
    cleaned = _CREDENTIAL_HEADER.sub(r"\1: ****", str(text or ""))
    cleaned = _BEARER.sub("Bearer ****", cleaned)
    cleaned = _SECRET_FIELD.sub(r"\1=****", cleaned)
    cleaned = _URL.sub("[redacted endpoint]", cleaned)
    return _CONTROL.sub(" ", cleaned).strip()


def _failure_note(server: McpServerRecord, exc: BaseException) -> str:
    """One sanitized line about one failed server: slug plus exception class.

    Client errors are already built from fixed text and the server id, so their text
    is worth keeping; anything else is reduced to its class name, and every note is
    redacted again before it is handed back.
    """
    label = _server_label(server)
    detail = str(exc) if isinstance(exc, McpServerError) else type(exc).__name__
    note = truncate_error("{0}: {1}".format(label, sanitize_sync_error(detail)))
    return note or "{0} tool sync failed".format(label)


def _carried(
    server: McpServerRecord, rows: Sequence[McpToolRecord]
) -> list[McpToolRecord]:
    """The rows of a server this snapshot could not or did not read.

    The definition is data and is kept exactly as stored; only the runtime flags
    follow the server, so a disabled or unreachable server's tools stay inspectable
    and drop out of tool_search/tool_execute.  These records are never in seen_ids,
    which is also what stops the store from moving last_seen_at: it keeps the
    timestamp of the last real sighting.
    """
    owner = server.owner_user_id
    return [
        replace(
            row,
            server_name=server.name,
            owner_user_id=owner,
            enabled=bool(server.enabled),
            reachable=False,
        )
        for row in rows
        if row.owner_user_id == owner
    ]


def _mapping(value: Any) -> dict[str, Any]:
    """A plain dict copy of an MCP payload field ({} when there is nothing usable)."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _annotations(tool: Any) -> dict[str, Any]:
    """Tool.annotations as the JSON object the column stores."""
    value = getattr(tool, "annotations", None)
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _mapping(dump(mode="json", by_alias=True, exclude_none=True))
        except TypeError:  # a non-Pydantic stand-in without the extra arguments
            return _mapping(dump())
    return _mapping(value)


def _count(snapshot: Any, field: str) -> int:
    """A counter from whatever the store returned, tolerant of a test double."""
    return int(getattr(snapshot, field, 0) or 0)


def _ids(snapshot: Any, field: str) -> tuple[str, ...]:
    return tuple(str(value) for value in getattr(snapshot, field, ()) or ())


class McpCatalogSync:
    """Discover the tools of every stored MCP server and publish one snapshot.

    sync() is the only entry point: it is called at startup, by the daily fallback,
    by a manual refresh and by the API after a server write.  It does not lock or
    debounce - the runtime that owns those triggers does.
    """

    def __init__(
        self,
        server_store: McpServerSource,
        tool_store: McpToolSink,
        client: McpToolLister,
        index: McpToolIndex | None = None,
        embedder: Any = None,
        *,
        cipher: HeaderCipher | None = None,
        connection_factory: McpConnectionFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
    ) -> None:
        self._server_store = server_store
        self._tool_store = tool_store
        self._client = client
        # The index is optional in both halves: without an index *or* an embedder
        # there is nothing to rebuild, and the sync stays a pure database write.
        self._index = index
        self._embedder = embedder
        self._cipher = cipher
        self._connection_factory = connection_factory
        self._clock = clock
        self._max_document_chars = max_document_chars

    async def sync(self, reason: str = "manual", full: bool = False) -> McpSyncStats:
        """Read every server and publish one complete tool snapshot.

        reason is for logs only (startup, daily, server_refresh, ...) and full is
        forwarded to the index, which may use it to rebuild from scratch.
        """
        started = self._clock()
        servers = list(await self._server_store.list_all())
        rows = list(await self._tool_store.list_all())
        rows_by_server: dict[str, list[McpToolRecord]] = {}
        for row in rows:
            rows_by_server.setdefault(row.server_id, []).append(row)

        records: list[McpToolRecord] = []
        seen_ids: set[str] = set()
        notes: list[str] = []
        conflicts: list[str] = []
        servers_failed = 0

        for server in servers:
            server_rows = rows_by_server.get(server.id, [])
            if not server.enabled:
                # Switched off: keep what it offered, publish nothing, stay offline.
                records.extend(_carried(server, server_rows))
                continue
            try:
                connection = self._connection_for(server)
                tools = list(await self._client.list_tools(connection))
            except Exception as exc:  # noqa: BLE001 - one server must not stop the rest
                servers_failed += 1
                notes.append(_failure_note(server, exc))
                logger.warning(
                    "MCP server %s tool sync failed (%s)",
                    server.id,
                    type(exc).__name__,
                )
                records.extend(_carried(server, server_rows))
                continue
            built, server_conflicts = self._records_for(server, tools, server_rows)
            conflicts.extend(server_conflicts)
            records.extend(built)
            seen_ids.update(record.id for record in built)

        # One call, one transaction: a per-server snapshot would retire every other
        # server's tools, so the complete list goes in together.
        snapshot = await self._tool_store.upsert_snapshot(records, seen_ids)

        index_failed = False
        if self._index is not None and self._embedder is not None:
            try:
                await self._index.sync(
                    await self._tool_store.list_all(),
                    embedder=self._embedder,
                    full=full,
                )
            except Exception as exc:  # noqa: BLE001 - the commit already happened
                index_failed = True
                kind = type(exc).__name__
                notes.append("MCP tool index sync failed ({0})".format(kind))
                logger.warning("MCP tool index sync failed (%s)", type(exc).__name__)

        logger.info(
            "MCP tool sync (%s) scanned %d servers (%d failed) and %d tools",
            reason,
            len(servers),
            servers_failed,
            len(records),
        )
        return McpSyncStats(
            scanned=len(records),
            servers_scanned=len(servers),
            servers_failed=servers_failed,
            changed=_count(snapshot, "inserted") + _count(snapshot, "updated"),
            upserted=len(_ids(snapshot, "upsert_ids")),
            deleted=len(_ids(snapshot, "delete_ids")),
            error=truncate_error("; ".join(notes)) if notes else None,
            index_failed=index_failed,
            duration_ms=max(0, int((self._clock() - started) * 1000)),
            conflicts=tuple(conflicts),
        )

    # -- snapshot construction --------------------------------------------- #

    def _records_for(
        self,
        server: McpServerRecord,
        tools: Sequence[mcp_types.Tool],
        server_rows: Sequence[McpToolRecord],
    ) -> tuple[list[McpToolRecord], list[str]]:
        """The records one reachable server contributes, and the conflicts it hit."""
        owner = server.owner_user_id
        stored_by_name = {row.name: row for row in server_rows}
        records: list[McpToolRecord] = []
        conflicts: list[str] = []
        handled: set[str] = set()
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or name in handled:
                # A blank name is not a tool; a duplicate is one tool advertised twice,
                # and the row identity is (server_id, name), so the first one wins.
                continue
            handled.add(name)
            stored = stored_by_name.get(name)
            if stored is not None and stored.owner_user_id != owner:
                # The store matches rows by (server_id, name) and writes the owner it
                # is handed, so rewriting this row would transfer somebody else's tool
                # to this owner. Skip it, keep the row, and report it.
                conflicts.append(call_name_for(server.name, name))
                logger.warning(
                    "MCP tool %r of server %s belongs to another owner; skipped",
                    name,
                    server.id,
                )
                continue
            records.append(self._record_for(server, tool, name))
        return records, conflicts

    def _record_for(
        self, server: McpServerRecord, tool: Any, name: str
    ) -> McpToolRecord:
        """One tools/list entry as the row the store will write."""
        schema = _mapping(getattr(tool, "inputSchema", None))
        description = str(getattr(tool, "description", "") or "")
        document = build_tool_document(
            name=name,
            title=getattr(tool, "title", None),
            description=description,
            server=server.name,
            input_schema=schema,
            max_chars=self._max_document_chars,
        )
        call_name = call_name_for(server.name, name)
        return McpToolRecord(
            id=tool_id_for(server.id, name),
            server_id=server.id,
            server_name=server.name,
            owner_user_id=server.owner_user_id,
            name=name,
            call_name=call_name,
            display_name=document.display_name,
            description=description,
            input_schema=schema,
            annotations=_annotations(tool),
            parameters=document.parameters,
            enabled=bool(server.enabled),
            reachable=True,
            # Callability is the store's call: it depends on seen_ids and its gate.
            content=document.content,
            content_hash=content_hash_for(
                document.content, server.owner_user_id, call_name
            ),
            schema_hash=schema_hash_for(schema),
        )

    # -- connections ------------------------------------------------------- #

    def _connection_for(self, server: McpServerRecord) -> McpServerConnection:
        """The connection the client will open for one server."""
        if self._connection_factory is not None:
            return self._connection_factory(server)
        return McpServerConnection(
            server_id=server.id,
            url=server.url,
            transport=server.transport,
            headers=self._headers_for(server),
            verify_tls=bool(server.verify_tls),
        )

    def _headers_for(self, server: McpServerRecord) -> dict[str, str]:
        """The plaintext headers of one server, or a failure the sync records.

        Decryption belongs here - the sync owns the cipher - and never in the client.
        A server whose headers cannot be read is *not* contacted without auth: that
        would fail confusingly at best, and could reach an endpoint as the wrong
        tenant at worst.
        """
        token = server.headers_encrypted
        if not token:
            return {}
        if self._cipher is None:
            raise McpServerConfigurationError(
                "stored headers cannot be used: no cipher is configured"
            )
        try:
            return dict(self._cipher.decrypt(token))
        except ValueError:
            raise McpServerConfigurationError(
                "stored headers could not be decrypted with the configured key"
            ) from None


__all__ = [
    "McpCatalogSync",
    "McpConnectionFactory",
    "McpServerSource",
    "McpSyncStats",
    "McpToolIndex",
    "McpToolLister",
    "McpToolSink",
    "sanitize_sync_error",
]
