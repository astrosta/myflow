"""One process-wide sync of the ContextForge tool catalog.

Every sync reads ContextForge twice and merges the two views:

* the MCP endpoint's tools/list is the execution authority - its `inputSchema`
  is stored verbatim, because a Pydantic round-trip would rewrite `$ref`,
  `anyOf` and `default`;
* Admin REST /tools is the management authority - it carries the tool id,
  display name, tags, enabled/reachable and the `myflow_owner` annotation.

The merged snapshot is written to `contextforge_tools` in a single transaction
and only then is the derived Milvus index rebuilt.  The database is the
authoritative runtime source, so a failed index update leaves the catalog intact
and only degrades retrieval, while a failed upstream read aborts the whole sync
and keeps the previous version in both places.

Three rules keep the merge honest:

* the tool name is the only join key;
* an MCP-only tool is never written: without an Admin row its id and owner are
  unknown, so it is skipped instead of given a guessed id (a stored row for that
  name still stops being callable, because the snapshot did not include it), and
  an ambiguous name is dropped rather than persisted as an arbitrary row;
* `callable` is never set here.  The store derives it from enabled/reachable/
  owner plus the `seen_names` of this snapshot, which is why the sync passes the
  names MCP actually listed and leaves the flag at its default.

The single asyncio.Lock lives in this module because the gateway runs exactly one
worker (GATEWAY_WORKERS=1).  Task 5's runtime calls this service for startup,
/admin/events, the midnight pass, tool-call failures and manual reindex; it must
not add a second lock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    """

    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    error: str | None = None


_process_lock: asyncio.Lock | None = None


def sync_lock() -> asyncio.Lock:
    """The one process-level sync lock.

    Created lazily so importing this module never needs a running loop.  With a
    single gateway worker the lock is the whole concurrency story: every sync
    trigger (startup, /admin/events, midnight, tool-call errors, manual reindex)
    funnels through it, so two snapshots can never interleave a DB write with
    another pass's Milvus rebuild.
    """
    global _process_lock
    if _process_lock is None:
        _process_lock = asyncio.Lock()
    return _process_lock


class ContextForgeSyncService:
    """Merge ContextForge's two views into the catalog, then rebuild the index."""

    def __init__(
        self,
        config: ContextForgeConfig,
        admin: ContextForgeAdminClient | None = None,
        mcp: ContextForgeMcpClient | None = None,
        store: ContextForgeToolStore | None = None,
        index: ToolVectorIndex | None = None,
        embedder: Embedder | None = None,
        *,
        page_size: int = DEFAULT_ADMIN_PAGE_SIZE,
        max_document_chars: int | None = None,
    ) -> None:
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

        Never raises for an upstream, database or index problem: an error is
        reported in the returned stats and the previous catalog stays in place,
        which is what keeps `sync` usable as a fire-and-forget refresh signal.

        `full=True` additionally drops every Milvus row the catalog no longer
        indexes (manual reindex); the database rows are never deleted, because a
        tool that disappears and comes back reuses its definition.
        """
        if not self._config.enabled:
            return ContextForgeSyncStats()
        async with sync_lock():
            return await self._sync_locked(reason=reason, full=full)

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
        if self._mcp is None:
            return []
        return list(await self._mcp.list_tools())

    async def _admin_tools(self) -> list[ContextForgeTool]:
        """Page Admin REST /tools until a short page says the catalog is over."""
        if self._admin is None:
            return []
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
        admin_by_name, admin_conflicts = _by_name(admin_tools)
        mcp_by_name, mcp_conflicts = _by_name(mcp_tools)
        conflicts = admin_conflicts | mcp_conflicts
        prior = _prior_rows(stored_rows)

        records: list[ContextForgeToolRecord] = []
        for name in sorted(admin_by_name):
            if name in conflicts:
                logger.warning(
                    "Dropping the ContextForge tool %r: its name is claimed more than once",
                    name,
                )
                continue
            record = self._record(
                admin_by_name[name],
                mcp_by_name.get(name),
                name=name,
                prior=prior,
            )
            if record is not None:
                records.append(record)

        for name in sorted(set(mcp_by_name) - set(admin_by_name)):
            logger.warning(
                "Skipping the ContextForge MCP tool %r: Admin /tools has no row, so "
                "its id and owner are unknown",
                name,
            )
        # Every name MCP listed is reported as seen; a name without a record simply
        # has nothing to derive callability for.
        return records, set(mcp_by_name)

    def _record(
        self,
        admin_tool: ContextForgeTool,
        mcp_tool: Any | None,
        *,
        name: str,
        prior: tuple[dict[str, ContextForgeToolRecord], dict[str, ContextForgeToolRecord]],
    ) -> ContextForgeToolRecord | None:
        """Build one row from the merge; None when the record cannot be trusted."""
        if not admin_tool.id:
            logger.warning("Dropping the ContextForge tool %r: Admin returned no id", name)
            return None
        prior_by_id, prior_by_name = prior
        previous = prior_by_id.get(admin_tool.id) or prior_by_name.get(name)
        if mcp_tool is not None:
            schema = _schema(mcp_tool)
            if schema is None:
                return None
            description = _description(mcp_tool, admin_tool)
        else:
            # MCP no longer lists it: the execution definition we last saw is the
            # only one there is, so keep it instead of blanking the row.
            schema = dict(previous.input_schema) if previous is not None else {}
            description = _carried_description(previous, admin_tool)

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
        return ContextForgeToolRecord(
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


def _aborted(
    reason: str, code: str, exc: Exception, *, scanned: int = 0
) -> ContextForgeSyncStats:
    """One failed sync: log it and keep the previous catalog in both stores."""
    logger.warning(
        "ContextForge sync(%s) aborted (%s): %s", reason, code, type(exc).__name__
    )
    return ContextForgeSyncStats(scanned=scanned, error=code)


def _by_name(tools: Sequence[Any]) -> tuple[dict[str, Any], set[str]]:
    """Index tools by name; a name claimed twice is ambiguous, not a winner."""
    by_name: dict[str, Any] = {}
    conflicts: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        if name in by_name:
            conflicts.add(name)
            continue
        by_name[name] = tool
    return by_name, conflicts


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
            "Dropping the ContextForge tool %r: %s",
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
