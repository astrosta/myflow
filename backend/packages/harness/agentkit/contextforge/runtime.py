"""Process-level ContextForge runtime: tool snapshot, index, events, status."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.admin import ContextForgeAdminClient
from agentkit.contextforge.admin_events import AdminEventSubscriber
from agentkit.contextforge.cache import (
    ContextForgeSnapshot,
    get_contextforge_snapshot,
    invalidate_contextforge_cache,
    set_contextforge_snapshot,
)
from agentkit.contextforge.catalog import build_entries
from agentkit.contextforge.index import ToolIndexSyncStats, ToolVectorIndex
from agentkit.contextforge.loader import load_contextforge_tools
from agentkit.tools.search.service import (
    ToolSearchService,
    reset_tool_search_runtime,
    set_tool_search_runtime,
)

logger = logging.getLogger(__name__)


class ContextForgeRuntime:
    def __init__(
        self,
        config: ContextForgeConfig,
        *,
        admin: ContextForgeAdminClient,
        index: ToolVectorIndex | None,
        embedder: Any | None = None,
        reranker: Any | None = None,
    ) -> None:
        self.config = config
        self.admin = admin
        self.index = index
        self.embedder = embedder
        self.reranker = reranker
        self.subscriber: AdminEventSubscriber | None = None
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._dirty = False
        self._started = False
        self._sync_status = "disabled" if not config.enabled else "ready"
        self._last_sync_at: str | None = None
        self._last_sync_reason: str | None = None
        self._last_sync_duration_ms: int | None = None
        self._last_error: str | None = None
        self._last_index_stats: ToolIndexSyncStats | None = None
        self._reachable = False
        self._auth_ok = False
        self._last_retry_at: str | None = None
        self._last_retry_tool: str | None = None
        self._last_retry_result: str | None = None

    async def start(self) -> None:
        if self._started or not self.config.enabled:
            return
        self._started = True
        try:
            if self.index is not None:
                await self.index.ensure_collection(self.config.search.embedding.dimensions)
            await self.refresh_and_sync(reason="startup")
        except Exception as exc:  # noqa: BLE001 - startup never blocks the gateway
            logger.warning("ContextForge initial sync failed: %s", type(exc).__name__)
            self._sync_status = "degraded"
            self._last_error = type(exc).__name__
        if self.config.admin_events.enabled:
            self.subscriber = AdminEventSubscriber(
                self.config,
                self._on_admin_event,
            )
            try:
                await self.subscriber.start()
            except Exception:  # noqa: BLE001
                logger.warning("ContextForge admin event subscriber failed", exc_info=True)

    async def stop(self) -> None:
        if self.subscriber is not None:
            await self.subscriber.stop()
            self.subscriber = None
        task = self._refresh_task
        self._refresh_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.index is not None:
            await self.index.aclose()
        await self.admin.aclose()
        reset_tool_search_runtime()
        self._started = False

    async def _on_admin_event(self, event: Any) -> None:
        del event
        self.request_refresh_async("admin_events")

    async def request_refresh(self, reason: str) -> None:
        """Refresh after an admin write, honoring the configured sync mode."""
        if not self.config.enabled:
            return
        if self.config.sync.admin_refresh_async:
            self.request_refresh_async(reason)
            return
        await self.refresh_and_sync(reason=reason)

    def request_refresh_async(self, reason: str) -> None:
        if not self.config.enabled:
            return
        self._dirty = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._refresh_worker(reason),
                name="contextforge-refresh",
            )

    async def _refresh_worker(self, reason: str) -> None:
        while self._dirty:
            self._dirty = False
            try:
                await self.refresh_and_sync(reason=reason)
            except Exception as exc:  # noqa: BLE001 - status carries the failure
                logger.warning("ContextForge refresh failed: %s", type(exc).__name__)
                self._sync_status = "degraded"
                self._last_error = type(exc).__name__

    async def refresh_and_sync(
        self,
        *,
        reason: str,
        force: bool = False,
        timeout: float | None = None,
        full: bool = False,
    ) -> ToolIndexSyncStats:
        del force
        timeout = timeout or self.config.sync.sync_timeout_seconds
        async with self._lock:
            self._sync_status = "syncing"
            started = asyncio.get_running_loop().time()
            try:
                stats = await asyncio.wait_for(
                    self._refresh_locked(reason=reason, full=full),
                    timeout=timeout,
                )
                self._sync_status = "ready" if stats.error is None else "degraded"
                self._last_index_stats = stats
                self._last_error = stats.error
                self._reachable = True
                self._auth_ok = True
                return stats
            except asyncio.TimeoutError:
                self._sync_status = "degraded"
                self._last_error = "timeout"
                return ToolIndexSyncStats(error="timeout")
            except Exception as exc:  # noqa: BLE001
                self._sync_status = "error"
                self._last_error = type(exc).__name__
                raise
            finally:
                self._last_sync_at = datetime.now(UTC).isoformat()
                self._last_sync_reason = reason
                self._last_sync_duration_ms = int(
                    (asyncio.get_running_loop().time() - started) * 1000
                )

    async def _refresh_locked(self, *, reason: str, full: bool) -> ToolIndexSyncStats:
        del reason
        tools = await load_contextforge_tools(self.config)
        enrichment = await self._enrichment(tools)
        entries = build_entries(
            tools,
            enrichment=enrichment,
            max_document_chars=self.config.search.max_document_chars,
        )
        set_contextforge_snapshot(tools=tools, entries=entries)
        if self.index is None or self.embedder is None:
            return ToolIndexSyncStats(scanned=len(entries))
        return await self.index.sync(entries, embedder=self.embedder, full=full)

    async def _enrichment(self, tools: list[Any]) -> dict[str, dict[str, Any]]:
        if not self.config.admin_token:
            return {}
        try:
            remote = await self.admin.list_tools(limit=200, skip=0, include_inactive=True)
        except Exception:  # noqa: BLE001 - enrichment is optional
            return {}
        return {
            tool.name: {
                "display_name": tool.display_name,
                "tags": list(tool.tags),
            }
            for tool in remote
        }

    async def reindex(self) -> ToolIndexSyncStats:
        stats = await self.refresh_and_sync(reason="manual", full=True)
        return stats

    def get_tools(self) -> tuple[Any, ...]:
        snapshot = get_contextforge_snapshot()
        return snapshot.tools if snapshot is not None else ()

    def get_entries(self) -> tuple[Any, ...]:
        snapshot = get_contextforge_snapshot()
        return snapshot.entries if snapshot is not None else ()

    def snapshot(self) -> ContextForgeSnapshot | None:
        return get_contextforge_snapshot()

    def status(self) -> dict[str, Any]:
        snapshot = get_contextforge_snapshot()
        entries = snapshot.entries if snapshot is not None else ()
        return {
            "enabled": self.config.enabled,
            "reachable": self._reachable,
            "auth_ok": self._auth_ok,
            "admin_events": self.subscriber.status if self.subscriber else "disabled",
            "sync": self._sync_status,
            "index": "ready" if self.index is not None and snapshot is not None else "disabled",
            "indexed_tools": len(entries),
            "total_tools": len(entries),
            "last_sync_at": self._last_sync_at,
            "last_sync_reason": self._last_sync_reason,
            "last_error": self._last_error,
            "last_sync_duration_ms": self._last_sync_duration_ms,
            "last_retry_at": self._last_retry_at,
            "last_retry_tool": self._last_retry_tool,
            "last_retry_result": self._last_retry_result,
            "channels": {
                "keyword": "ready" if self.config.search.keyword.enabled else "disabled",
                "embedding": "ready" if self.embedder is not None else "disabled",
                "rerank": "ready" if self.reranker is not None else "disabled",
            },
        }

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


def build_contextforge_runtime(config: ContextForgeConfig) -> ContextForgeRuntime | None:
    if not config.enabled:
        return None
    admin = ContextForgeAdminClient(config)
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
    except Exception:  # noqa: BLE001 - retrieval degrades to local BM25
        logger.warning("ContextForge vector index unavailable; using local fallback", exc_info=True)
    runtime = ContextForgeRuntime(
        config,
        admin=admin,
        index=index,
        embedder=embedder,
        reranker=reranker,
    )
    configure_contextforge_runtime(runtime)
    if config.search.enabled:
        set_tool_search_runtime(
            ToolSearchService(
                index=index,
                embedder=embedder,
                reranker=reranker,
                config=config.search,
                snapshot_provider=runtime.get_entries,
            )
        )
    return runtime


__all__ = [
    "ContextForgeRuntime",
    "build_contextforge_runtime",
    "configure_contextforge_runtime",
    "get_contextforge_runtime",
    "reset_contextforge_runtime",
]
