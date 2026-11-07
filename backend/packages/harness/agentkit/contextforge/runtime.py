"""Process-level ContextForge runtime: when to sync, and what the process knows.

The catalog itself lives in contextforge_tools plus the derived Milvus index, and
the Task 4 ContextForgeSyncService is the only writer.  This module owns the
*triggers* and the process lifecycle:

* one startup sync, so the first request already sees the catalog;
* one /admin/events subscriber - the only real-time external update signal;
* one debounced, single-flight refresh worker, so a burst of events becomes one
  sync and an event that lands during a sync causes exactly one follow-up pass;
* one local-midnight task, the fallback full tools/list when no event arrives;
* manual reindex, and the status surface.

Two rules keep that honest.  The runtime never takes a lock of its own: the sync
service already serialises every trigger per event loop, and a second lock would
only create a place to deadlock.  And the runtime keeps no tool snapshot: status
counts come from the authoritative store (list_all is the admin read), so a
restart or another writer can never leave the process reporting a catalog it no
longer has.  The single-worker guard in the app layer is what makes one
subscriber and one midnight task sufficient (rev.9 6.3/6.4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.admin import ContextForgeAdminClient
from agentkit.contextforge.admin_events import (
    AdminEventSubscriber,
    should_refresh_tool_event,
)
from agentkit.contextforge.daily import daily_delay as compute_daily_delay
from agentkit.contextforge.mcp_client import ContextForgeMcpClient
from agentkit.contextforge.models import ContextForgeAdminEvent
from agentkit.contextforge.store import ContextForgeToolStore
from agentkit.contextforge.sync import ContextForgeSyncService, ContextForgeSyncStats
from agentkit.persistence.engine import get_session_factory

logger = logging.getLogger(__name__)

#: sync() error codes that mean the snapshot was not replaced at all because
#: ContextForge or the database failed.  Any other error came from the derived
#: index, which degrades retrieval but leaves the catalog in place.
_UPSTREAM_ABORT_CODES = frozenset(
    {"mcp_failed", "admin_failed", "db_failed", "lock_failed"}
)

SubscriberFactory = Callable[[ContextForgeConfig, Any], AdminEventSubscriber]
DailyDelay = Callable[[], float]


class ContextForgeRuntime:
    """Owns the ContextForge sync triggers for one process (one worker)."""

    def __init__(
        self,
        config: ContextForgeConfig,
        *,
        sync: ContextForgeSyncService,
        admin: ContextForgeAdminClient,
        index: Any | None = None,
        embedder: Any | None = None,
        reranker: Any | None = None,
        store: ContextForgeToolStore | None = None,
        subscriber_factory: SubscriberFactory | None = None,
        daily_delay: DailyDelay | None = None,
    ) -> None:
        self.config = config
        self.admin = admin
        self.reranker = reranker
        self._sync = sync
        self._store = store if store is not None else getattr(sync, "store", None)
        self._index = index if index is not None else getattr(sync, "index", None)
        self._embedder = (
            embedder if embedder is not None else getattr(sync, "embedder", None)
        )
        self._subscriber_factory: SubscriberFactory = (
            subscriber_factory or AdminEventSubscriber
        )
        self._daily_delay = daily_delay
        self.subscriber: AdminEventSubscriber | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._daily_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._started = False
        self._dirty = False
        self._pending_reason = "admin_events"
        self._sync_status = "disabled" if not config.enabled else "ready"
        self._last_sync_at: str | None = None
        self._last_sync_reason: str | None = None
        self._last_sync_duration_ms: int | None = None
        self._last_sync_monotonic: float | None = None
        self._last_error: str | None = None
        self._index_error: str | None = None
        self._last_counts = (0, 0)
        self._reachable = False
        self._auth_ok = False
        self._last_retry_at: str | None = None
        self._last_retry_tool: str | None = None
        self._last_retry_result: str | None = None

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Sync once, subscribe to /admin/events, then schedule local midnight."""
        if self._started or not self.config.enabled:
            return
        self._started = True
        self._stop.clear()
        if self._index is not None:
            try:
                await self._index.ensure_collection(
                    self.config.search.embedding.dimensions
                )
            except Exception as exc:  # noqa: BLE001 - retrieval degrades, tools stay
                self._index_error = type(exc).__name__
                logger.warning("ContextForge index collection check failed: %s", exc)
        if self.config.sync.startup_sync:
            await self._sync_once("startup")
        if self.config.admin_events.enabled:
            self.subscriber = self._subscriber_factory(self.config, self._on_admin_event)
            try:
                await self.subscriber.start()
            except Exception:  # noqa: BLE001 - events are best effort
                logger.warning("ContextForge admin event subscriber failed", exc_info=True)
        self._daily_task = asyncio.create_task(
            self._daily_loop(), name="contextforge-daily"
        )

    async def stop(self) -> None:
        """Cancel the tasks, stop the subscriber, and release the clients."""
        if not self._started:
            return
        self._started = False
        self._stop.set()
        await self._cancel_daily_task()
        subscriber, self.subscriber = self.subscriber, None
        if subscriber is not None:
            with suppress(Exception):
                await subscriber.stop()
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._dirty = False
        if self._index is not None:
            with suppress(Exception):
                await self._index.aclose()
        with suppress(Exception):
            await self.admin.aclose()

    async def _cancel_daily_task(self) -> None:
        task, self._daily_task = self._daily_task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    # -- triggers ---------------------------------------------------------- #

    async def request_refresh(self, reason: str) -> None:
        """Sync after a local management write, per the configured mode.

        The write already succeeded, so this never raises: an upstream or index
        problem is a status, not an error the router has to translate.
        """
        if not self.config.enabled:
            return
        if self.config.sync.admin_refresh_async:
            self.request_refresh_async(reason)
            return
        await self._sync_once(reason)

    def request_refresh_async(self, reason: str) -> None:
        """Mark the catalog dirty and start the refresh worker when it is idle."""
        if not self.config.enabled:
            return
        self._dirty = True
        self._pending_reason = reason or self._pending_reason
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._drain_refresh(), name="contextforge-refresh"
            )

    async def reindex(self) -> ContextForgeSyncStats:
        """Manual reindex: a full sync, so the derived index is pruned too."""
        if not self.config.enabled:
            return ContextForgeSyncStats()
        return await self._sync_once("manual", full=True)

    async def _on_admin_event(self, event: ContextForgeAdminEvent) -> None:
        """An event is a signal only; never a tool definition."""
        if not should_refresh_tool_event(event.type):
            return
        self.request_refresh_async("admin_events")

    async def _drain_refresh(self) -> None:
        """One debounced pass, plus one more if a signal arrived mid-sync."""
        while True:
            delay = self._debounce_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            # The sync reads ContextForge live, so signals raised during the
            # debounce window are already covered by the pass about to run; only
            # signals that arrive while it runs need a second one.
            self._dirty = False
            await self._sync_once(self._pending_reason)
            if not self._dirty:
                return

    def _debounce_seconds(self) -> float:
        """Debounce window, floored by the minimum interval between syncs."""
        events = self.config.admin_events
        delay = max(0.0, events.debounce_ms / 1000.0)
        if self._last_sync_monotonic is not None:
            elapsed = asyncio.get_running_loop().time() - self._last_sync_monotonic
            delay = max(delay, float(events.refresh_min_interval_seconds) - elapsed)
        return delay

    async def _daily_loop(self) -> None:
        """Sleep until local daily_at + jitter, then run one full sync."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._next_daily_delay())
                return
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            await self._sync_once("midnight", full=True)

    def _next_daily_delay(self) -> float:
        if self._daily_delay is not None:
            return max(0.0, float(self._daily_delay()))
        sync_config = self.config.sync
        return compute_daily_delay(
            datetime.now(UTC),
            timezone=sync_config.timezone,
            daily_at=sync_config.daily_at,
            jitter_seconds=sync_config.daily_jitter_seconds,
        )

    # -- one sync ---------------------------------------------------------- #

    async def _sync_once(
        self, reason: str, *, full: bool = False
    ) -> ContextForgeSyncStats:
        """Run exactly one sync through the service and record what it did."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        self._sync_status = "syncing"
        try:
            stats = await self._sync.sync(reason, full=full)
        except Exception as exc:  # noqa: BLE001 - the service documents no raise
            logger.warning("ContextForge sync(%s) raised: %s", reason, type(exc).__name__)
            stats = ContextForgeSyncStats(error=type(exc).__name__)
        finished = loop.time()
        self._last_sync_at = datetime.now(UTC).isoformat()
        self._last_sync_reason = reason
        self._last_sync_duration_ms = int((finished - started) * 1000)
        self._last_sync_monotonic = finished
        self._last_error = stats.error
        self._sync_status = "ready" if stats.error is None else "degraded"
        if stats.error is None:
            self._reachable = True
            self._auth_ok = True
            self._index_error = None
        elif stats.error in _UPSTREAM_ABORT_CODES:
            # The snapshot was not replaced: ContextForge or the database failed.
            self._reachable = False
            self._auth_ok = False
        else:
            # tools/list and the Admin read both succeeded and the snapshot was
            # committed; only the derived index failed.  Reporting the catalog as
            # unreachable here would send an operator to check ContextForge for a
            # Milvus problem, so reachability follows the upstream reads.
            self._reachable = True
            self._auth_ok = True
            self._index_error = stats.error
        return stats

    # -- status ------------------------------------------------------------ #

    async def status(self) -> dict[str, Any]:
        """The management surface: store counts, never a stale snapshot."""
        callable_count, total = self._last_counts
        catalog_state = "ready"
        if self._store is not None:
            try:
                rows = await self._store.list_all(None)
            except Exception as exc:  # noqa: BLE001 - status must still answer
                catalog_state = "degraded"
                logger.warning(
                    "ContextForge status catalog read failed: %s", type(exc).__name__
                )
            else:
                total = len(rows)
                callable_count = sum(
                    1 for row in rows if bool(getattr(row, "callable", False))
                )
                self._last_counts = (callable_count, total)
        return {
            "enabled": self.config.enabled,
            "reachable": self._reachable,
            "auth_ok": self._auth_ok,
            "admin_events": self.subscriber.status if self.subscriber else "disabled",
            "daily": self._daily_state(),
            "sync": self._sync_status,
            "index": self._index_state(catalog_state),
            "indexed_tools": callable_count,
            "total_tools": total,
            "last_sync_at": self._last_sync_at,
            "last_sync_reason": self._last_sync_reason,
            "last_error": self._last_error,
            "last_sync_duration_ms": self._last_sync_duration_ms,
            "last_retry_at": self._last_retry_at,
            "last_retry_tool": self._last_retry_tool,
            "last_retry_result": self._last_retry_result,
            "channels": {
                "keyword": "ready" if self.config.search.keyword.enabled else "disabled",
                "embedding": "ready" if self._embedder is not None else "disabled",
                "rerank": "ready" if self.reranker is not None else "disabled",
            },
        }

    def _daily_state(self) -> str:
        if not self.config.enabled:
            return "disabled"
        task = self._daily_task
        return "running" if task is not None and not task.done() else "stopped"

    def _index_state(self, catalog_state: str) -> str:
        if catalog_state != "ready":
            return "degraded"
        if self._index is None:
            return "disabled"
        if self._index_error is not None:
            return "degraded"
        return "ready"

    def record_retry(self, *, tool_name: str, result: str) -> None:
        self._last_retry_at = datetime.now(UTC).isoformat()
        self._last_retry_tool = tool_name
        self._last_retry_result = result


_runtime: ContextForgeRuntime | None = None
_runtime_config: ContextForgeConfig | None = None


def configure_contextforge_runtime(runtime: ContextForgeRuntime | None) -> None:
    global _runtime, _runtime_config
    _runtime = runtime
    _runtime_config = runtime.config if runtime is not None else None


def get_contextforge_runtime() -> ContextForgeRuntime | None:
    return _runtime


def reset_contextforge_runtime() -> None:
    global _runtime, _runtime_config
    _runtime = None
    _runtime_config = None


def build_contextforge_runtime(
    config: ContextForgeConfig,
    *,
    session_factory: Any | None = None,
) -> ContextForgeRuntime | None:
    """Build the process runtime, or None when ContextForge is off.

    The store is mandatory: contextforge_tools is the runtime authority, so a
    process without a database has no catalog to own and must not pretend to
    have one.
    """
    if not config.enabled:
        return None
    factory = session_factory if session_factory is not None else get_session_factory()
    if factory is None:
        logger.warning(
            "ContextForge runtime needs the persistent catalog; no database session "
            "factory is configured"
        )
        return None
    admin = ContextForgeAdminClient(config)
    mcp = ContextForgeMcpClient(config)
    store = ContextForgeToolStore(factory)
    index = None
    embedder = None
    reranker = None
    try:
        if config.search.enabled:
            from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
            from agentkit.knowledge.rerank import build_reranker
            from agentkit.contextforge.index import ToolVectorIndex

            embedder = OpenAICompatibleEmbedder(config.search.embedding)
            index = ToolVectorIndex(
                config.search.vector_store,
                dimensions=config.search.embedding.dimensions,
                keyword=config.search.keyword,
                rrf_k=config.search.rrf_k,
            )
            reranker = build_reranker(config.search.rerank)
    except Exception:  # noqa: BLE001 - retrieval degrades to the local fallback
        logger.warning(
            "ContextForge vector index unavailable; using local fallback", exc_info=True
        )
        index = None
        embedder = None
        reranker = None
    service = ContextForgeSyncService(config, admin, mcp, store, index, embedder)
    runtime = ContextForgeRuntime(
        config,
        sync=service,
        admin=admin,
        index=index,
        embedder=embedder,
        reranker=reranker,
        store=store,
    )
    configure_contextforge_runtime(runtime)
    return runtime


__all__ = [
    "ContextForgeRuntime",
    "build_contextforge_runtime",
    "configure_contextforge_runtime",
    "get_contextforge_runtime",
    "reset_contextforge_runtime",
]
