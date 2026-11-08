"""Process-level MCP tool runtime: the sync triggers, the status, and the calls.

`mcp_tools` is the authoritative catalog and `McpCatalogSync` is its only
writer, so this module owns exactly the things the sync deliberately does not:

* **when a pass runs** - one startup pass (so the first request already sees a
  catalog), one local-midnight pass (the fallback for a server that changed
  without telling us), a management write's refresh, `tool_execute`'s
  refresh-after-failure, and the manual reindex;
* **serialization** - one process-wide `asyncio.Lock` around every pass.  The
  sync itself takes no lock (its docstring says so), so a burst of two triggers
  must not start two snapshots: `request_refresh` coalesces them into one pass,
  and a trigger that lands while a pass is running causes exactly one follow-up;
* **the server stamps** - the sync must not write `mcp_servers` (it walks every
  owner and cannot tell whether its own pass completed), so this runtime maps
  `McpSyncStats.failed_servers` back onto the rows: a failed server becomes
  `error`/unreachable with its sanitized note, every enabled server that did
  not fail becomes `active`/reachable with a fresh `last_tools_sync_at`, and a
  disabled server is left alone because nothing contacted it;
* **the call path** - `call_tool` is the only place a `tools/call` leaves this
  process.  It re-reads the server row (owner-scoped), refuses a disabled or
  unreachable server, decrypts the stored headers with the process cipher, and
  runs the tool under its *upstream* name on the server that owns it.

Two rules keep `tool_execute` honest.  `refresh_server` returns the outcome
for **the one server it was given** - never the catalog-wide summary - because
the executor uses it to decide whether the re-read that follows saw a fresh
snapshot: a whole-catalog success would claim a target that failed, and another
server's failure would veto the retry of a healthy one.  And `stop()` retires
the process-global handles only if this runtime still owns them, so stopping a
replaced runtime cannot blind the newer one.

The single-worker guard in the app layer is what makes one daily task and one
lock per process sufficient; a second worker would run its own startup and
midnight pass over the same catalog.

`build_mcp_runtime` is where a process becomes callable: it builds the stores,
the client, the optional retrieval stack and the sync, publishes the runtime
through `runtime_protocol.set_mcp_runtime` (what `tool_execute` reads on every
call) and installs the tool_search service (what `tool_search` reads on every
call).  A disabled block publishes neither, so a deployment that turned MCP
servers off answers `mcp_unavailable` instead of reaching for a catalog it does
not own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp import types as mcp_types

from agentkit.config.mcp_servers_config import McpServersConfig
from agentkit.config.tool_search_config import ToolSearchConfig
from agentkit.mcp.client import McpServerClient, McpServerConnection
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.errors import (
    McpServerConfigurationError,
    McpServerNotFound,
    McpServerTimeout,
    McpServerUnavailable,
    McpServerValidationError,
    McpToolError,
    sanitize_error_message,
)
from agentkit.mcp.models import McpServerRecord
from agentkit.mcp.runtime_protocol import (
    get_mcp_runtime,
    reset_mcp_runtime,
    set_mcp_runtime,
)
from agentkit.mcp.service import McpServerReindexStats
from agentkit.mcp.store.repository import McpServerStore
from agentkit.mcp.store.tool_repository import McpToolRecord, McpToolStore
from agentkit.mcp.sync import McpCatalogSync, McpSyncStats
from agentkit.persistence.engine import get_session_factory

logger = logging.getLogger(__name__)

DailyDelay = Callable[[], float]
#: Call failures that say "the server is not usable right now" rather than "this
#: tool call failed".  They degrade the stored server row; a McpToolError does not,
#: because the server answered and only the call was refused.
_DEGRADING_ERRORS = (
    McpServerUnavailable,
    McpServerTimeout,
    McpServerConfigurationError,
)


@dataclass(frozen=True, slots=True)
class McpServerSyncResult:
    """The outcome of one refresh, for **the one server it was asked about**.

    `tool_execute` reads exactly two members: `error` (this server's failure,
    or None when its snapshot was replaced) and `index_failed` (the database
    snapshot was committed and only the derived index failed, so the catalog the
    executor reads is already fresh).  Reporting the catalog-wide summary here
    instead would make one server's failure veto another's retry.
    """

    server_id: str = ""
    error: str | None = None
    index_failed: bool = False


@dataclass(frozen=True, slots=True)
class _Pass:
    """One sync pass plus whether the pass itself aborted.

    The sync service reports per-server failures inside its stats, so `error`
    alone cannot say "the snapshot was not replaced at all": a pass that raised
    (the database is gone, say) has no per-server entry either.  The flag keeps
    the two apart for the per-target result.
    """

    stats: McpSyncStats
    aborted: bool = False


def _elapsed_ms(started: float) -> int:
    return max(0, int((asyncio.get_running_loop().time() - started) * 1000))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def seconds_until_local_midnight(now: datetime | None = None) -> float:
    """Seconds until the next local midnight, for the daily fallback pass.

    `.astimezone()` hands back a fixed-offset aware datetime, so the arithmetic
    below stays valid (a DST transition day is off by an hour, which is why the
    design calls this a fallback and not a schedule).  No config knob is invented
    for it: the design names local midnight and nothing else.
    """
    current = now.astimezone() if now is not None else datetime.now().astimezone()
    next_midnight = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (next_midnight - current).total_seconds())


class McpToolRuntime:
    """Owns the MCP sync triggers, the server stamps, and one process's calls."""

    def __init__(
        self,
        config: McpServersConfig,
        sync: McpCatalogSync,
        server_store: McpServerStore,
        tool_store: McpToolStore,
        mcp_client: McpServerClient,
        cipher: HeaderCipher,
        index: Any | None = None,
        embedder: Any | None = None,
        reranker: Any | None = None,
        tool_search: ToolSearchConfig | None = None,
        daily_delay: DailyDelay | None = None,
    ) -> None:
        self.config = config
        self.sync = sync
        self.server_store = server_store
        self.tool_store = tool_store
        self.mcp_client = mcp_client
        # The management service composes with these two, and the cipher must be
        # the same one the sync decrypts with: two ciphers would mean two keys.
        self.cipher = cipher
        self.index = index
        self.embedder = embedder
        self.reranker = reranker
        #: The retrieval policy `tool_execute` falls back to; None keeps the
        #: design default (require_retrieved=true), because an unreadable policy is
        #: not permission.
        self.tool_search = tool_search
        #: The refresh-and-retry switch (design 7).  `mcp_servers` deliberately
        #: carries no retry policy, so this is a runtime attribute, default on.
        self.retry_on_tool_error = True
        self._daily_delay = daily_delay
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._started = False
        #: Set by stop().  A pass started after shutdown would touch a database
        #: the lifespan is about to close, so every trigger answers "not now".
        self._stopped = False
        self._dirty = False
        self._pending_reason = "manual"
        self._refresh_task: asyncio.Task[None] | None = None
        self._daily_task: asyncio.Task[None] | None = None
        self._sync_state = "disabled" if not config.enabled else "ready"
        self._last_sync_at: str | None = None
        self._last_sync_reason: str | None = None
        self._last_sync_duration_ms: int | None = None
        self._last_error: str | None = None
        self._index_error: str | None = None
        self._last_retry_at: str | None = None
        self._last_retry_tool: str | None = None
        self._last_retry_result: str | None = None

    # -- tool_execute's inputs --------------------------------------------- #

    @property
    def store(self) -> McpToolStore:
        """Alias of `tool_store`; the catalog row the executor calls by."""
        return self.tool_store

    @property
    def require_retrieved(self) -> bool:
        """Whether a call name must have been retrieved in this run (design 8)."""
        return bool(getattr(self.tool_search, "require_retrieved", True))

    def _wants_passes(self) -> bool:
        """Whether a trigger should start a pass at all (enabled and not stopped)."""
        return bool(self.config.enabled) and not self._stopped

    def record_retry(self, *, tool_name: str, result: str) -> None:
        """Book one refresh-and-retry outcome, for the catalog-wide status."""
        self._last_retry_at = _utc_now().isoformat()
        self._last_retry_tool = tool_name
        self._last_retry_result = result

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Prepare the index, run the startup pass, then schedule local midnight."""
        if self._started or not self.config.enabled:
            return
        self._started = True
        self._stopped = False
        self._stop.clear()
        await self._ensure_index()
        # Always one startup pass: `mcp_servers` has no sync block, and a catalog
        # that is only built at the first management write is a catalog the first
        # request cannot search.
        await self._sync_pass("startup")
        self._daily_task = asyncio.create_task(self._daily_loop(), name="mcp-daily")

    async def stop(self) -> None:
        """Cancel the tasks, then release the index and the client."""
        if not self.config.enabled:
            return
        self._started = False
        self._stopped = True
        self._stop.set()
        await self._cancel(self._daily_task)
        self._daily_task = None
        await self._cancel(self._refresh_task)
        self._refresh_task = None
        self._dirty = False
        # The whole retrieval stack is this runtime's to release: an embedder and
        # a reranker hold HTTP clients bound to this loop, and closing only the
        # Milvus client would leak them until the loop shuts down.
        await self._close_resource(self.index)
        await self._close_resource(self.embedder)
        await self._close_resource(self.reranker)
        await self._close_resource(self.mcp_client)
        # Only retire the process-global handles if this runtime still owns them:
        # stopping an old runtime must not clear a newer one that replaced it.
        if get_mcp_runtime() is self:
            reset_mcp_runtime()
            _set_tool_search_runtime(None)

    @staticmethod
    async def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    @staticmethod
    async def _close_resource(resource: Any | None) -> None:
        """Close one owned resource if it is closeable; never raise on the way out."""
        if resource is None:
            return
        for name in ("aclose", "close"):
            close = getattr(resource, name, None)
            if callable(close):
                with suppress(Exception):
                    await close()
                return

    # -- triggers ---------------------------------------------------------- #

    async def request_refresh(self, reason: str) -> None:
        """Ask for one pass in the background, coalescing with whatever is queued.

        The caller is a management write whose row is already committed, so this
        never blocks on the sync and never raises: an upstream or index problem
        is a status, not an error the router has to translate.
        """
        if not self._wants_passes():
            return
        self._dirty = True
        self._pending_reason = reason or self._pending_reason
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._drain_refresh(), name="mcp-refresh"
            )

    async def refresh_now(self, reason: str = "manual") -> McpSyncStats:
        """One awaited pass under the process lock, whatever else is running.

        `tool_execute`'s fallback cannot hand the sync to the background worker:
        it has to see the refreshed catalog before it retries, so it would race
        its own re-read.  After `stop()` there is no process runtime to refresh
        against - the database is on its way out - so it answers an empty pass
        instead of a failing one.
        """
        if not self._wants_passes():
            return McpSyncStats()
        return (await self._sync_pass(reason)).stats

    async def refresh_server(self, server_id: str) -> McpServerSyncResult:
        """Refresh the catalog and report the outcome **for this server only**.

        This is what `tool_execute` calls after a missing tool or a schema
        mismatch.  The pass is catalog-wide because the tool store is a
        full-snapshot writer (a per-server snapshot would retire every other
        server's tools), but the *result* is per target.
        """
        return await self.sync_server(server_id, reason="server_refresh")

    async def sync_server(
        self, server_id: str, reason: str = "server_refresh"
    ) -> McpServerSyncResult:
        """The `McpServerSync` protocol the management service composes with.

        `McpServerService` calls this after every write and re-reads the row
        afterwards, so the pass has to stamp `mcp_servers` - which it does, for
        every server the pass walked.
        """
        if not self.config.enabled:
            return McpServerSyncResult(server_id=server_id, error="mcp_disabled")
        if self._stopped:
            # "Not refreshed" is the honest answer: nothing ran, so a caller must
            # not treat the stored snapshot as fresh.
            return McpServerSyncResult(server_id=server_id, error="mcp_stopped")
        pass_ = await self._sync_pass(reason)
        return self._target_result(pass_, server_id)

    async def remove_server_catalog(self, server_id: str) -> tuple[str, ...]:
        """Delete a deleted server's tool rows and their index documents.

        Deleting a server deletes its tools (spec 4.2 rule 3).  The rows go first
        because they are the authority; the derived index then drops the same ids,
        and an index failure is logged instead of raised - the database row that
        would have listed those tools is already gone either way.

        The delete runs **under the pass lock**.  A sync that was already walking
        the server list still holds that server's rows in its snapshot and will
        rewrite them at commit time, so deleting outside the lock lets an in-flight
        pass resurrect a catalog this call just removed.  Taking the lock makes the
        two orders the only two possible ones: a pass commits first and this delete
        removes what it wrote, or this delete runs first and the next pass no
        longer sees the server at all.
        """
        async with self._lock:
            ids = tuple(await self.tool_store.delete_by_server(server_id))
            if ids and self.index is not None:
                try:
                    await self.index.delete_by_ids(list(ids))
                except Exception as exc:  # noqa: BLE001 - the catalog is clean
                    logger.warning(
                        "MCP tool index cleanup failed for server %s (%s)",
                        server_id,
                        type(exc).__name__,
                    )
            return ids

    async def reindex(self, owner_user_id: str | None = None) -> McpServerReindexStats:
        """Manual full reindex: one pass with `full=True` for the index rebuild.

        The pass is catalog-wide because the tool store's snapshot is: it retires
        every row the pass did not rewrite, so an owner-scoped snapshot would be
        data loss.  The owner is still checked, because this is a per-user route
        and an empty owner there is a caller bug.
        """
        if not self.config.enabled:
            return McpServerReindexStats()
        if owner_user_id is not None and not str(owner_user_id).strip():
            raise McpServerValidationError(
                "owner_user_id is required for a manual reindex"
            )
        stats = (await self._sync_pass("manual", full=True)).stats
        return McpServerReindexStats(
            scanned=stats.scanned,
            changed=stats.changed,
            upserted=stats.upserted,
            deleted=stats.deleted,
            failed=stats.servers_failed,
            duration_ms=stats.duration_ms,
            error=stats.error,
        )

    async def _drain_refresh(self) -> None:
        """One pass, plus one more if a trigger arrived while it ran."""
        while True:
            self._dirty = False
            await self._sync_pass(self._pending_reason)
            if not self._dirty:
                return

    async def _daily_loop(self) -> None:
        """Sleep until local midnight, then run one full pass."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._next_daily_delay()
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            await self._sync_pass("daily", full=True)

    def _next_daily_delay(self) -> float:
        if self._daily_delay is not None:
            return max(0.0, float(self._daily_delay()))
        return seconds_until_local_midnight()

    # -- one pass ---------------------------------------------------------- #

    async def _sync_pass(self, reason: str, *, full: bool = False) -> _Pass:
        """Run exactly one catalog pass under the process lock, then stamp."""
        async with self._lock:
            started = asyncio.get_running_loop().time()
            self._sync_state = "syncing"
            if self._index_error is not None:
                # A collection check that failed at startup is not evidence the
                # index is still broken: retry it here, so the flag can only be
                # cleared by a check that actually succeeded.
                await self._ensure_index()
            # Which servers this pass is responsible for, read *before* the sync
            # walks them: a server created while the pass runs was never contacted
            # by it, so it must not be stamped from this pass's result.  Only the
            # ids are kept - the stamp itself re-reads the rows afterwards, because
            # their enabled flag may have changed while the pass was in flight.
            walked = {row.id for row in await self._server_rows()}
            aborted = False
            try:
                stats = await self.sync.sync(reason, full=full)
            except Exception as exc:  # noqa: BLE001 - reported as status, never raised
                # The sync documents no raise; if the database itself is gone it
                # still must not take a request (or a started gateway) down.
                aborted = True
                stats = McpSyncStats(
                    error=type(exc).__name__, duration_ms=_elapsed_ms(started)
                )
                logger.warning(
                    "MCP tool sync (%s) raised %s", reason, type(exc).__name__
                )
            if not aborted:
                await self._stamp_servers(stats, walked)
            self._record(reason, stats, started)
            logger.info(
                "MCP tool sync (%s) done: %d servers, %d failed, %d tools",
                reason,
                stats.servers_scanned,
                stats.servers_failed,
                stats.scanned,
            )
            return _Pass(stats=stats, aborted=aborted)

    def _record(self, reason: str, stats: McpSyncStats, started: float) -> None:
        """Remember what this pass did, for the status surface."""
        self._last_sync_at = _utc_now().isoformat()
        self._last_sync_reason = reason
        self._last_sync_duration_ms = stats.duration_ms or _elapsed_ms(started)
        self._last_error = stats.error
        if stats.index_failed:
            # The database commit went through and only Milvus failed, so the
            # catalog is live; the index is what degrades.
            self._index_error = stats.error or "index_failed"
        # A pass that reports no index failure does not clear an index error: a
        # pass without a retrieval stack never touches Milvus, and the index step
        # swallows its own failures, so "no error reported" is not "the index
        # recovered".  Only _ensure_index() clears the flag, on a check that
        # actually succeeded.
        self._sync_state = "ready" if stats.error is None else "degraded"

    def _target_result(self, pass_: _Pass, server_id: str) -> McpServerSyncResult:
        """This server's outcome from a catalog-wide pass (see `refresh_server`)."""
        failure = next(
            (item for item in pass_.stats.failed_servers if item.server_id == server_id),
            None,
        )
        if failure is not None:
            return McpServerSyncResult(
                server_id=server_id, error=failure.error, index_failed=False
            )
        if pass_.aborted:
            # No snapshot was written at all, so this target did not refresh -
            # even though the pass named no failed server.
            return McpServerSyncResult(
                server_id=server_id,
                error=pass_.stats.error or "sync_failed",
                index_failed=False,
            )
        if pass_.stats.index_failed:
            # The commit landed; only the derived index failed, so a re-read sees
            # the fresh snapshot and tool_execute must treat this as refreshed.
            return McpServerSyncResult(
                server_id=server_id,
                error=pass_.stats.error,
                index_failed=True,
            )
        # Another server's failure is in `stats.error` and is not ours: this
        # target's rows were rewritten by the same full snapshot.
        return McpServerSyncResult(server_id=server_id)

    async def _server_rows(self) -> list[McpServerRecord]:
        """Every server row, for the pass to stamp; empty when unreadable.

        Best effort on purpose: if the rows cannot be read the catalog pass will
        fail on its own, and an empty list means "stamp nothing", which never
        claims a server this process did not contact.
        """
        try:
            return list(await self.server_store.list_all())
        except Exception as exc:  # noqa: BLE001 - the pass reports the failure
            logger.warning("MCP server read for stamping failed (%s)", type(exc).__name__)
            return []

    async def _stamp_servers(
        self, stats: McpSyncStats, walked_ids: set[str]
    ) -> None:
        """Write each server's status from the pass that just ran.

        Two rules, and they are about two different races around one snapshot:

        * `walked_ids` is what the pass was responsible for, read before the sync
          ran.  A server created *during* the pass is absent from it, so it is not
          stamped: nothing contacted it, and claiming "active" would publish a row
          that was never verified (its own create-triggered pass stamps it);
        * the rows themselves are read **after** the pass.  A server switched off
          while the pass was in flight is `enabled=False` by then, and the disable
          must win: stamping the pre-pass copy would flip a disabled server back to
          `active`/reachable and undo a management write.  Any row that is not
          enabled now is left exactly as the write left it.

        Success is derived, not reported: every enabled walked server the pass did
        not name as failed was contacted and answered.
        """
        failed = {item.server_id: item.error for item in stats.failed_servers}
        stamp = _utc_now()
        # Fresh read: the pass may have taken a while, and the user's switch is
        # newer than the snapshot the pass used.
        for server in await self._server_rows():
            if server.id not in walked_ids:
                # Created during the pass; nothing contacted it.
                continue
            error = failed.get(server.id)
            if not server.enabled:
                # Switched off - before or during the pass - so the disable stands:
                # nothing contacted this server and nothing may revive it.
                continue
            try:
                if error is None:
                    await self.server_store.set_status(
                        server.id,
                        server.owner_user_id,
                        status="active",
                        reachable=True,
                        last_error=None,
                        last_connected_at=stamp,
                        last_tools_sync_at=stamp,
                    )
                else:
                    await self.server_store.set_status(
                        server.id,
                        server.owner_user_id,
                        status="error",
                        reachable=False,
                        last_error=error,
                    )
            except Exception as exc:  # noqa: BLE001 - one row must not stop the rest
                logger.warning(
                    "MCP server %s status stamp failed (%s)",
                    server.id,
                    type(exc).__name__,
                )

    # -- status ------------------------------------------------------------ #

    async def status(self, owner_user_id: str | None = None) -> dict[str, Any]:
        """Store counts plus this process's sync state; never a cached snapshot.

        `owner_user_id` scopes the counts to one caller's rows (the /status
        route), and `None` is the catalog-wide read.
        """
        servers, server_state = await self._servers(owner_user_id)
        tools, tool_state = await self._tools(owner_user_id)
        catalog_state = (
            "ready" if server_state == tool_state == "ready" else "degraded"
        )
        last_sync = max(
            (row.last_tools_sync_at for row in servers if row.last_tools_sync_at),
            default=None,
        )
        first_server_error = next(
            (row.last_error for row in servers if row.last_error), None
        )
        # The process-wide error belongs to whoever caused it: a scoped caller may
        # only see a failure of *their own* rows, because the catalog-wide note
        # names another user's server and the upstream reason it failed.
        scoped = owner_user_id is not None
        status: dict[str, Any] = {
            "enabled": bool(self.config.enabled),
            "servers_total": len(servers),
            "servers_active": sum(1 for row in servers if row.status == "active"),
            "servers_pending": sum(1 for row in servers if row.status == "pending"),
            "servers_error": sum(1 for row in servers if row.status == "error"),
            "servers_disabled": sum(1 for row in servers if row.status == "disabled"),
            "servers_reachable": sum(1 for row in servers if row.reachable),
            "tools_total": len(tools),
            "indexed_tools": sum(1 for row in tools if row.callable),
            "last_sync_at": last_sync,
            "last_error": (
                first_server_error
                if scoped
                else (self._last_error or first_server_error)
            ),
            "sync": self._sync_state,
            "index": self._index_state(catalog_state),
            "last_sync_reason": self._last_sync_reason,
            "last_sync_duration_ms": self._last_sync_duration_ms,
        }
        if not scoped:
            # The retry record is process-global (the last tool-error fallback), so
            # it belongs on the catalog-wide view only: a scoped caller must not
            # learn which tool another user's retry touched.
            status.update(
                {
                    "process_last_sync_at": self._last_sync_at,
                    "last_retry_at": self._last_retry_at,
                    "last_retry_tool": self._last_retry_tool,
                    "last_retry_result": self._last_retry_result,
                }
            )
        return status

    async def _servers(
        self, owner_user_id: str | None
    ) -> tuple[list[McpServerRecord], str]:
        try:
            rows = (
                await self.server_store.list_all()
                if owner_user_id is None
                else await self.server_store.list_for_owner(owner_user_id)
            )
        except Exception as exc:  # noqa: BLE001 - status must still answer
            logger.warning("MCP server status read failed (%s)", type(exc).__name__)
            return [], "degraded"
        return list(rows or ()), "ready"

    async def _tools(
        self, owner_user_id: str | None
    ) -> tuple[list[McpToolRecord], str]:
        try:
            rows = await self.tool_store.list_all(owner_user_id)
        except Exception as exc:  # noqa: BLE001 - status must still answer
            logger.warning("MCP tool status read failed (%s)", type(exc).__name__)
            return [], "degraded"
        return list(rows or ()), "ready"

    def _index_state(self, catalog_state: str) -> str:
        if catalog_state != "ready":
            return "degraded"
        if self._index_error is not None:
            return "degraded"
        if self.index is None:
            # "disabled" is the operator's choice (tool_search off).  A search
            # block that is on while this process has no index is a failed build,
            # which is degraded - reporting "disabled" would hide the breakage.
            search_enabled = bool(getattr(self.tool_search, "enabled", False))
            return "degraded" if search_enabled else "disabled"
        return "ready"

    def _dimensions(self) -> int | None:
        embedding = getattr(self.tool_search, "embedding", None)
        dimensions = getattr(embedding, "dimensions", None)
        return int(dimensions) if dimensions else None

    def mark_index_error(self, reason: str) -> None:
        """Record a retrieval-stack problem found while the runtime was built.

        `build_mcp_runtime` calls this when the embedder/index/reranker could not
        be constructed: the catalog still works, so the runtime must run, but the
        status has to say "degraded" instead of pretending retrieval is simply off.
        """
        self._index_error = reason or "index_unavailable"

    async def _ensure_index(self) -> None:
        """Check the collection, and only then decide whether the index is healthy."""
        if self.index is None:
            return
        try:
            await self.index.ensure_collection(self._dimensions())
        except Exception as exc:  # noqa: BLE001 - retrieval degrades, tools stay
            self._index_error = type(exc).__name__
            logger.warning(
                "MCP tool index collection check failed: %s", type(exc).__name__
            )
        else:
            self._index_error = None

    # -- the call ---------------------------------------------------------- #

    async def call_tool(
        self,
        record: McpToolRecord,
        arguments: dict[str, Any],
    ) -> mcp_types.CallToolResult:
        """Run one stored tool on the server that owns it.

        `tool_execute` resolves the row and the server itself before calling, so
        these checks are the second pair of eyes on the same facts: a runtime
        called directly (a script, a future route) must not execute a tool the
        store no longer classifies as callable, or one whose server this owner
        does not have.
        """
        server = await self._callable_server(record)
        try:
            # Building the connection is part of the call: an undecryptable header
            # map is a broken server row, not a failed tool, and it must degrade
            # the row exactly like an unreachable endpoint does.
            connection = self._connection_for(server)
            return await self.mcp_client.call_tool(
                connection, record.name, dict(arguments or {})
            )
        except _DEGRADING_ERRORS as exc:
            # The server itself is what failed (unreachable, timed out, or its
            # stored headers cannot be used), so the row must say so: the next
            # search/execute sees the state the management page shows.  A tool-level
            # failure (McpToolError) means the server answered and only that call
            # failed, so it is deliberately not degraded here.
            await self._degrade_server(server, exc)
            raise

    async def _callable_server(self, record: McpToolRecord) -> McpServerRecord:
        """The server row behind a call, or the reason this call may not run."""
        if record is None:  # pragma: no cover - the executor never passes None
            raise McpToolError("MCP tool record is missing")
        owner = str(record.owner_user_id or "").strip()
        if not owner:
            raise McpToolError("MCP tool has no owner: tools are user-private")
        server = await self.server_store.get(record.server_id, owner)
        if server is None or server.owner_user_id != owner:
            # Another owner's server id is reported as "not found" on purpose: the
            # answer must not reveal that the row exists for somebody else.
            raise McpServerNotFound("MCP server not found")
        if not record.callable:
            raise McpToolError(f"MCP tool {record.name!r} is not callable for this user")
        if not server.enabled:
            raise McpServerUnavailable(f"MCP server {server.name!r} is disabled")
        if not server.reachable:
            raise McpServerUnavailable(f"MCP server {server.name!r} is unreachable")
        return server

    def _connection_for(self, server: McpServerRecord) -> McpServerConnection:
        """The session options for one call: URL, decrypted headers, timeouts."""
        return McpServerConnection(
            server_id=server.id,
            url=server.url,
            transport=server.transport,  # type: ignore[arg-type]
            headers=self._headers_for(server),
            verify_tls=bool(server.verify_tls),
            request_timeout_seconds=float(self.config.request_timeout_seconds),
            connect_timeout_seconds=float(self.config.connect_timeout_seconds),
        )

    async def _degrade_server(
        self, server: McpServerRecord, exc: BaseException
    ) -> None:
        """Mark one server error/unreachable after a transport-level failure.

        `last_error` is the shared sanitizer's output, so a message that quotes a
        URL or a header cannot reach the row.  A failed stamp never masks the tool
        result: the caller is about to receive the original error anyway.
        """
        try:
            await self.server_store.set_status(
                server.id,
                server.owner_user_id,
                status="error",
                reachable=False,
                last_error=sanitize_error_message(exc),
            )
        except Exception as stamp_exc:  # noqa: BLE001 - the call result wins
            logger.warning(
                "MCP server %s could not be marked unreachable (%s)",
                server.id,
                type(stamp_exc).__name__,
            )

    def _headers_for(self, server: McpServerRecord) -> dict[str, str]:
        """The plaintext headers of one server; never logged, never returned."""
        token = server.headers_encrypted
        if not token:
            return {}
        try:
            return dict(self.cipher.decrypt(token))
        except ValueError:  # HeaderCipher's single failure type, never the token
            raise McpServerConfigurationError(
                "stored MCP server headers could not be decrypted with the configured "
                "encryption key"
            ) from None


# --------------------------------------------------------------------------- #
# Process wiring
# --------------------------------------------------------------------------- #


def _set_tool_search_runtime(service: Any | None) -> None:
    # Imported lazily: the runtime is the lower layer, and only the startup hook
    # may pull the tool package in.
    from agentkit.tools.search.service import set_tool_search_runtime

    set_tool_search_runtime(service)


def build_mcp_runtime(
    config: McpServersConfig,
    session_factory: Any | None = None,
    *,
    tool_search: ToolSearchConfig | None = None,
) -> McpToolRuntime | None:
    """Build the process runtime, or None when MCP servers are off.

    The store is mandatory: `mcp_servers`/`mcp_tools` are the runtime
    authority, so a process without a database has no catalog to own and must not
    pretend to have one.  `validate_for_startup()` runs first even though the app
    preflights it too: a builder called from a script or a test must fail on a
    missing key *before* anything is published, not at the first header write.
    """
    if not config.enabled:
        # A disabled build must remove the previous process runtime as well as the
        # retrieval service; otherwise tool_execute keeps calling the old catalog
        # after the operator turned MCP servers off.
        reset_mcp_runtime()
        _set_tool_search_runtime(None)
        return None
    config.validate_for_startup()
    factory = session_factory if session_factory is not None else get_session_factory()
    if factory is None:
        logger.warning(
            "MCP tool runtime needs the persistent catalog; no database session "
            "factory is configured"
        )
        reset_mcp_runtime()
        _set_tool_search_runtime(None)
        return None
    search = tool_search if tool_search is not None else ToolSearchConfig()
    cipher = HeaderCipher(config.resolved_encryption_key())
    server_store = McpServerStore(factory)
    tool_store = McpToolStore(factory)
    client = McpServerClient()
    index: Any | None = None
    embedder: Any | None = None
    reranker: Any | None = None
    retrieval_error: str | None = None
    if bool(getattr(search, "enabled", False)):
        try:
            from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
            from agentkit.knowledge.rerank import build_reranker
            from agentkit.mcp.index import McpToolVectorIndex

            embedder = OpenAICompatibleEmbedder(search.embedding)
            index = McpToolVectorIndex(
                search.vector_store,
                dimensions=search.embedding.dimensions,
                keyword=search.keyword,
                rrf_k=search.rrf_k,
            )
            reranker = build_reranker(search.rerank)
        except Exception as exc:  # noqa: BLE001 - retrieval degrades, the catalog stays
            logger.warning(
                "MCP tool retrieval stack unavailable; tool_search will report it",
                exc_info=True,
            )
            retrieval_error = type(exc).__name__
            index = None
            embedder = None
            reranker = None
    sync = McpCatalogSync(server_store, tool_store, client, index, embedder, cipher=cipher)
    runtime = McpToolRuntime(
        config,
        sync,
        server_store,
        tool_store,
        client,
        cipher,
        index=index,
        embedder=embedder,
        reranker=reranker,
        tool_search=search,
    )
    if retrieval_error is not None:
        # The catalog runs, retrieval does not: status must say "degraded", not
        # "disabled" (which is the operator's switch, not a build failure).
        runtime.mark_index_error(retrieval_error)
    set_mcp_runtime(runtime)
    _install_tool_search(
        search,
        index=index,
        store=tool_store,
        embedder=embedder,
        reranker=reranker,
    )
    return runtime


def _install_tool_search(
    config: ToolSearchConfig,
    *,
    index: Any | None,
    store: McpToolStore,
    embedder: Any | None,
    reranker: Any | None,
) -> None:
    """Publish the built-in tool_search service for this process.

    The built-in reads one process-global service, so it is built where the
    authoritative store and the derived index are.  A disabled search block
    publishes None rather than a service with no channels: the tool then reports
    retrieval as unavailable instead of promising a path this process lacks.
    """
    if not bool(getattr(config, "enabled", False)):
        _set_tool_search_runtime(None)
        return
    from agentkit.tools.search.service import McpToolSearchService

    _set_tool_search_runtime(
        McpToolSearchService(
            config,
            index=index,
            store=store,
            embedder=embedder,
            reranker=reranker,
        )
    )


__all__ = [
    "McpServerSyncResult",
    "McpToolRuntime",
    "build_mcp_runtime",
    "seconds_until_local_midnight",
]
