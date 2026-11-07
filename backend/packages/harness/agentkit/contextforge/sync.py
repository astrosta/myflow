"""One process-wide sync of the ContextForge tool catalog.

Every sync reads ContextForge twice and merges the two views:

* the MCP endpoint's tools/list is the execution authority - its `inputSchema`
  is stored verbatim, because a Pydantic round-trip would rewrite `$ref`,
  `anyOf` and `default`;
* Admin REST /tools is the management authority - it carries the tool id,
  display name, tags, enabled/reachable and the `myflow_owner` annotation.

Both views are required: an empty tools/list is indistinguishable from a missing
client, so a sync that ran without one of them would retire the whole catalog,
and the service refuses to be constructed without them.

The merged snapshot is written to `contextforge_tools` in a single transaction
and only then is the derived Milvus index rebuilt.  The database is the
authoritative runtime source, so a failed index update leaves the catalog intact
and only degrades retrieval, while a failed upstream read aborts the whole sync
and keeps the previous version in both places.

Four rules keep the merge honest:

* the tool name is the only join key;
* repeat rows for one tool are one tool - the same Admin id listed twice (a
  paginated read racing a concurrent write) or the same MCP definition listed
  twice dedupe, and only two *different* identities claiming one name are
  ambiguous;
* an MCP-only tool is never written: without an Admin row its id and owner are
  unknown, so it is skipped instead of given a guessed id (a stored row for that
  name still stops being callable, because the snapshot did not include it);
* an invalid tool - an ambiguous name, an unparsable inputSchema - keeps a row
  with the definition it was last known to have.  `callable` is never set here:
  the store derives it from enabled/reachable/owner plus the `seen_names` of this
  snapshot, so the merge reports only the names it fully trusts and leaves the
  flag at its default.

The single asyncio.Lock lives in this module because the gateway runs exactly one
worker (GATEWAY_WORKERS=1).  It is created per running event loop: an
`asyncio.Lock` binds itself to the loop it is first used in and raises
`RuntimeError` in any other one, while `sync()` is documented never to raise, so
each loop gets its own lock instead (still one lock per loop, so every trigger on
that loop shares it).  Task 5's runtime calls this service for startup,
/admin/events, the midnight pass, tool-call failures and manual reindex; it must
not add a second lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.catalog import build_tool_content, parameter_names
from agentkit.contextforge.index import ToolIndexSyncStats
from agentkit.contextforge.models import (
    ContextForgeProtocolError,
    ContextForgeTool,
    normalize_input_schema,
)
from agentkit.contextforge.ownership import owner_from_annotations
from agentkit.contextforge.store import (
    ContextForgeToolRecord,
    ContextForgeToolStore,
    call_name_for,
    content_hash_for,
    schema_hash_for,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph flat
    from agentkit.contextforge.admin import ContextForgeAdminClient
    from agentkit.contextforge.index import Embedder, ToolVectorIndex
    from agentkit.contextforge.mcp_client import ContextForgeMcpClient

logger = logging.getLogger(__name__)

#: Admin REST /tools page size.  The sync pages with skip/limit until a short
#: page comes back; there is no fixed `limit=200` enrichment snapshot any more.
DEFAULT_ADMIN_PAGE_SIZE = 100
#: A guard against an endpoint that keeps returning full pages forever.
_MAX_ADMIN_PAGES = 500


@dataclass(frozen=True, slots=True)
class ContextForgeSyncStats:
    """What one sync did.

    `scanned` is the size of the merged snapshot, `changed` counts the rows the
    snapshot actually rewrote (inserted, updated, or retired to non-callable),
    `upserted`/`deleted` are the Milvus operations that followed the commit
    (both zero when retrieval is disabled), and `error` is set when the snapshot
    was not replaced - the previous catalog is still in place.

    `duration_ms` is how long the whole sync took, so the management surface can
    report it (`/api/tools/reindex`) instead of inventing it.

    `index_failed` says *where* an `error` came from: True means the DB snapshot
    was committed and only the derived Milvus index failed, so the catalog is
    live and only retrieval is degraded.  False alongside an `error` means the
    snapshot itself was not replaced (upstream, database, lock, or an exception
    the service could not classify), and the caller must not report the catalog
    as reachable.
    """

    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    error: str | None = None
    duration_ms: int = 0
    index_failed: bool = False


#: One lock per event loop that ever ran a sync.  A lock holds a strong reference
#: to the loop it is bound to, so closed loops are dropped when a new one appears
#: (see `_forget_closed_loops`) instead of accumulating for the process lifetime.
_process_locks: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()


def sync_lock() -> asyncio.Lock:
    """The process-level sync lock for the running event loop.

    Created lazily so importing this module never needs a running loop.  With a
    single gateway worker (and therefore a single loop) this is the whole
    concurrency story: every sync trigger (startup, /admin/events, midnight,
    tool-call errors, manual reindex) funnels through it, so two snapshots can
    never interleave a DB write with another pass's Milvus rebuild.

    The lock is per loop because `asyncio.Lock` binds to the loop it first runs
    in: handing the same lock to a second loop raises `RuntimeError` instead of
    serialising anything.
    """
    loop = asyncio.get_running_loop()
    lock = _process_locks.get(loop)
    if lock is not None:
        return lock
    _forget_closed_loops()
    lock = asyncio.Lock()
    _process_locks[loop] = lock
    return lock


def _forget_closed_loops() -> None:
    """Drop the locks of loops that have been closed.

    A lock references its loop, so a closed loop would otherwise be kept alive by
    this module for the rest of the process (one per pytest event loop).  A
    closed loop can never run again, so forgetting its lock loses nothing.
    """
    for loop in [key for key in _process_locks if key.is_closed()]:
        del _process_locks[loop]


class ContextForgeSyncService:
    """Merge ContextForge's two views into the catalog, then rebuild the index."""

    def __init__(
        self,
        config: ContextForgeConfig,
        admin: ContextForgeAdminClient,
        mcp: ContextForgeMcpClient,
        store: ContextForgeToolStore,
        index: ToolVectorIndex | None = None,
        embedder: Embedder | None = None,
        *,
        page_size: int = DEFAULT_ADMIN_PAGE_SIZE,
        max_document_chars: int | None = None,
    ) -> None:
        if admin is None or mcp is None:
            raise ValueError(
                "admin and mcp are required: a sync must read both ContextForge views, "
                "otherwise an empty one would retire the whole catalog"
            )
        if store is None:
            raise ValueError(
                "store is required: contextforge_tools is the authoritative catalog"
            )
        if int(page_size) <= 0:
            raise ValueError("page_size must be positive")
        self._config = config
        self._admin = admin
        self._mcp = mcp
        self._store = store
        self._index = index
        self._embedder = embedder
        self._page_size = int(page_size)
        self._max_document_chars = int(
            max_document_chars or config.search.max_document_chars
        )

    # -- exposed for the runtime (Task 5) ---------------------------------- #

    @property
    def config(self) -> ContextForgeConfig:
        return self._config

    @property
    def store(self) -> ContextForgeToolStore:
        return self._store

    @property
    def index(self) -> ToolVectorIndex | None:
        return self._index

    @property
    def embedder(self) -> Embedder | None:
        return self._embedder

    # -- sync -------------------------------------------------------------- #

    async def sync(self, reason: str = "manual", full: bool = False) -> ContextForgeSyncStats:
        """Refresh the catalog from ContextForge and return what changed.

        Never raises for an upstream, database, index or lock problem: an error
        is reported in the returned stats and the previous catalog stays in
        place, which is what keeps `sync` usable as a fire-and-forget refresh
        signal.

        `full=True` additionally drops every Milvus row the catalog no longer
        indexes (manual reindex); the database rows are never deleted, because a
        tool that disappears and comes back reuses its definition.
        """
        if not self._config.enabled:
            return ContextForgeSyncStats()
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            lock = sync_lock()
        except Exception as exc:  # noqa: BLE001 - sync must not raise at the caller
            return _timed(_aborted(reason, "lock_failed", exc), started, loop)
        async with lock:
            stats = await self._sync_locked(reason=reason, full=full)
        return _timed(stats, started, loop)

    async def reindex(self) -> ContextForgeSyncStats:
        """Manual reindex: a full sync, so the derived index is pruned too."""
        return await self.sync("manual", full=True)

    async def _sync_locked(self, *, reason: str, full: bool) -> ContextForgeSyncStats:
        try:
            mcp_tools = await self._mcp_tools()
        except Exception as exc:  # noqa: BLE001 - surfaced as an error stat
            return _aborted(reason, "mcp_failed", exc)
        try:
            admin_tools = await self._admin_tools()
        except Exception as exc:  # noqa: BLE001
            return _aborted(reason, "admin_failed", exc)
        try:
            stored_rows = await self._store.list_all(None)
        except Exception as exc:  # noqa: BLE001
            return _aborted(reason, "db_failed", exc)

        records, seen_names = self._merge(admin_tools, mcp_tools, stored_rows)
        try:
            snapshot = await self._store.upsert_snapshot(records, seen_names)
        except Exception as exc:  # noqa: BLE001 - the DB rejected the snapshot
            return _aborted(reason, "db_failed", exc, scanned=len(records))

        index_stats = await self._sync_index(full=full)
        stats = ContextForgeSyncStats(
            scanned=len(records),
            changed=snapshot.inserted + snapshot.updated + snapshot.disabled,
            upserted=index_stats.upserted,
            deleted=index_stats.deleted,
            error=index_stats.error,
            # The snapshot is committed at this point, so an index error degrades
            # retrieval only.  The runtime reads this flag instead of guessing
            # from the error code.
            index_failed=index_stats.error is not None,
        )
        logger.info(
            "ContextForge sync(%s): %d tools, %d changed, %d indexed, %d pruned%s",
            reason,
            stats.scanned,
            stats.changed,
            stats.upserted,
            stats.deleted,
            "" if stats.error is None else f", error={stats.error}",
        )
        return stats

    # -- inputs ------------------------------------------------------------ #

    async def _mcp_tools(self) -> list[Any]:
        return list(await self._mcp.list_tools())

    async def _admin_tools(self) -> list[ContextForgeTool]:
        """Page Admin REST /tools until a short page says the catalog is over."""
        tools: list[ContextForgeTool] = []
        skip = 0
        for _ in range(_MAX_ADMIN_PAGES):
            page = list(
                await self._admin.list_tools(
                    limit=self._page_size,
                    skip=skip,
                    include_inactive=True,
                )
            )
            tools.extend(page)
            if len(page) < self._page_size:
                return tools
            skip += len(page)
        raise ContextForgeProtocolError(
            "ContextForge Admin /tools kept returning full pages; pagination is broken"
        )

    # -- merge ------------------------------------------------------------- #

    def _merge(
        self,
        admin_tools: Sequence[ContextForgeTool],
        mcp_tools: Sequence[Any],
        stored_rows: Sequence[ContextForgeToolRecord],
    ) -> tuple[list[ContextForgeToolRecord], set[str]]:
        """Join the two views by name.

        Returns the rows to write and the names this snapshot fully trusts.  A
        name outside that set keeps its row but can never be callable, because
        the store derives `callable` - and the `last_seen_at` stamp - from what
        the snapshot saw upstream.  An invalid record therefore keeps the stamp
        of its last trusted sighting, which is the closest the store can express
        to "present but unusable" without a second writer.
        """
        admin_by_name, admin_conflicts = _by_name(admin_tools)
        mcp_by_name, mcp_conflicts = _by_name(mcp_tools)
        prior = _prior_rows(stored_rows)

        for name in sorted(mcp_conflicts):
            logger.warning(
                "ContextForge MCP lists %d different tools named %r: the stored "
                "definition is kept and the tool cannot be callable",
                len(mcp_conflicts[name]),
                name,
            )

        records: list[ContextForgeToolRecord] = []
        seen_names: set[str] = set()
        for name in sorted(set(admin_by_name) | set(admin_conflicts)):
            if name in admin_by_name:
                admin_tool = admin_by_name[name]
            else:
                admin_tool = _pick(admin_conflicts[name])
                logger.warning(
                    "ContextForge Admin claims %r for %d different tool ids: keeping %r, "
                    "which can never be callable",
                    name,
                    len(admin_conflicts[name]),
                    admin_tool.id,
                )
            record, defined_by_mcp = self._record(
                admin_tool,
                mcp_by_name.get(name),
                name=name,
                prior=prior,
            )
            if record is None:
                continue
            records.append(record)
            if defined_by_mcp and name not in admin_conflicts:
                seen_names.add(name)

        for name in sorted(set(mcp_by_name) - set(admin_by_name)):
            logger.warning(
                "Skipping the ContextForge MCP tool %r: Admin /tools has no row, so "
                "its id and owner are unknown",
                name,
            )
        return records, seen_names

    def _record(
        self,
        admin_tool: ContextForgeTool,
        mcp_tool: Any | None,
        *,
        name: str,
        prior: tuple[dict[str, ContextForgeToolRecord], dict[str, ContextForgeToolRecord]],
    ) -> tuple[ContextForgeToolRecord | None, bool]:
        """Build one row from the merge.

        Returns the record plus whether MCP supplied a trustworthy definition for
        it.  A row is still built when MCP did not (missing, ambiguous or
        unparsable): the last stored definition is carried forward and the name
        stays out of `seen_names`, so the store keeps the tool non-callable
        instead of dropping it from the catalog.
        """
        if not admin_tool.id:
            logger.warning("Skipping the ContextForge tool %r: Admin returned no id", name)
            return None, False
        prior_by_id, prior_by_name = prior
        previous = prior_by_id.get(admin_tool.id) or prior_by_name.get(name)
        defined_by_mcp = False
        schema: dict[str, Any] = {}
        if mcp_tool is not None:
            parsed = _schema(mcp_tool)
            if parsed is not None:
                schema = parsed
                defined_by_mcp = True
                description = _description(mcp_tool, admin_tool)
            else:
                description = _carried_description(previous, admin_tool)
        else:
            description = _carried_description(previous, admin_tool)
        if not defined_by_mcp and previous is not None:
            # MCP no longer lists the tool, or the definition it sent is not
            # usable: the execution definition we last saw is the only one there
            # is, so keep it instead of blanking the row.
            schema = dict(previous.input_schema)

        display_name = (admin_tool.display_name or "").strip() or None
        tags = tuple(str(tag) for tag in (admin_tool.tags or ()))
        parameters = parameter_names(schema)
        owner = owner_from_annotations(admin_tool.annotations) or admin_tool.owner
        call_name = call_name_for(name)
        content = build_tool_content(
            name=name,
            display_name=display_name or name,
            description=description,
            tags=tags,
            parameters=parameters,
            max_chars=self._max_document_chars,
        )
        record = ContextForgeToolRecord(
            id=admin_tool.id,
            name=name,
            call_name=call_name,
            display_name=display_name,
            description=description,
            input_schema=schema,
            annotations=dict(admin_tool.annotations or {}),
            tags=tags,
            parameters=parameters,
            owner_user_id=owner,
            enabled=bool(admin_tool.enabled),
            reachable=bool(admin_tool.reachable),
            # callable stays at its default: the store derives it from this
            # snapshot and these fields, so a writer cannot publish anything.
            content=content,
            content_hash=content_hash_for(content, owner, call_name),
            schema_hash=schema_hash_for(schema),
        )
        return record, defined_by_mcp

    # -- derived index ----------------------------------------------------- #

    async def _sync_index(self, *, full: bool) -> ToolIndexSyncStats:
        """Rebuild Milvus from the committed rows, never from the merge result."""
        if self._index is None or self._embedder is None:
            return ToolIndexSyncStats()
        try:
            rows = await self._store.list_all(None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ContextForge catalog read before indexing failed: %s", type(exc).__name__
            )
            return ToolIndexSyncStats(error="catalog_read_failed")
        try:
            return await self._index.sync(rows, embedder=self._embedder, full=full)
        except Exception as exc:  # noqa: BLE001 - the catalog is already committed
            logger.warning("ContextForge tool index sync failed: %s", type(exc).__name__)
            return ToolIndexSyncStats(scanned=len(rows), error=type(exc).__name__)


def _timed(
    stats: ContextForgeSyncStats, started: float, loop: asyncio.AbstractEventLoop
) -> ContextForgeSyncStats:
    """Stamp how long the sync took; the caller reports it (0 on same-millisecond runs)."""
    return replace(stats, duration_ms=max(0, int((loop.time() - started) * 1000)))


def _aborted(
    reason: str, code: str, exc: Exception, *, scanned: int = 0
) -> ContextForgeSyncStats:
    """One failed sync: log it and keep the previous catalog in both stores."""
    logger.warning(
        "ContextForge sync(%s) aborted (%s): %s", reason, code, type(exc).__name__
    )
    return ContextForgeSyncStats(scanned=scanned, error=code)


def _name_of(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "").strip()


def _identity(tool: Any) -> str:
    """What makes two rows one tool: the Admin id, or the MCP definition.

    MCP tools carry no id, so two entries with one name are the same tool only
    when they describe the same thing - which is exactly the duplicate a
    paginated read produces.
    """
    tool_id = str(getattr(tool, "id", "") or "").strip()
    if tool_id:
        return "id:" + tool_id
    payload = {
        "schema": getattr(tool, "inputSchema", None),
        "description": str(getattr(tool, "description", "") or ""),
    }
    return "definition:" + json.dumps(payload, sort_keys=True, default=str)


def _by_name(tools: Sequence[Any]) -> tuple[dict[str, Any], dict[str, tuple[Any, ...]]]:
    """Index tools by name, collapsing repeat rows of one tool.

    Returns the unambiguous tools by name plus, for each name claimed by two or
    more *different* tools, all the candidates.  A repeated identity is not a
    conflict: the same Admin tool listed twice by a paginated read that raced a
    concurrent write still names one tool, and treating that as ambiguity would
    retire a perfectly healthy tool until the next sync.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for tool in tools:
        name = _name_of(tool)
        if not name:
            continue
        grouped.setdefault(name, {}).setdefault(_identity(tool), tool)
    by_name: dict[str, Any] = {}
    conflicts: dict[str, tuple[Any, ...]] = {}
    for name, candidates in grouped.items():
        if len(candidates) == 1:
            by_name[name] = next(iter(candidates.values()))
        else:
            conflicts[name] = tuple(candidates.values())
    return by_name, conflicts


def _pick(candidates: Sequence[Any]) -> Any:
    """The row to keep for an ambiguous name: the smallest id, so it is stable."""
    return min(candidates, key=lambda tool: str(getattr(tool, "id", "") or ""))


def _prior_rows(
    rows: Sequence[ContextForgeToolRecord],
) -> tuple[dict[str, ContextForgeToolRecord], dict[str, ContextForgeToolRecord]]:
    """The stored catalog by id and by name, for carrying a definition forward."""
    return ({row.id: row for row in rows}, {row.name: row for row in rows})


def _schema(mcp_tool: Any) -> dict[str, Any] | None:
    """The MCP inputSchema, verbatim; None when it is not a usable schema."""
    try:
        return dict(normalize_input_schema(getattr(mcp_tool, "inputSchema", None)))
    except ContextForgeProtocolError as exc:
        logger.warning(
            "ContextForge MCP sent an unusable inputSchema for %r (%s); the stored "
            "definition is kept and the tool cannot be callable",
            str(getattr(mcp_tool, "name", "") or ""),
            exc,
        )
        return None


def _description(mcp_tool: Any, admin_tool: ContextForgeTool) -> str:
    """MCP's description when it has one; both views share the same source."""
    text = str(getattr(mcp_tool, "description", "") or "")
    return (text or admin_tool.description or "").strip()


def _carried_description(
    previous: ContextForgeToolRecord | None, admin_tool: ContextForgeTool
) -> str:
    if previous is not None:
        stored = str(previous.description or "").strip()
        if stored:
            return stored
    return str(admin_tool.description or "").strip()


__all__ = [
    "DEFAULT_ADMIN_PAGE_SIZE",
    "ContextForgeSyncService",
    "ContextForgeSyncStats",
    "sync_lock",
]
