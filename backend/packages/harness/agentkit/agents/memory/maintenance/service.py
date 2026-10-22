"""Periodic memory maintenance (M10 section 2.9)."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from agentkit.agents.memory.maintenance.folding import (
    FoldResult,
    build_folded_record,
    candidate_clusters,
    fold_cluster,
)
from agentkit.agents.memory.manager import MemoryManager
from agentkit.agents.memory.maintenance.policies import archive_candidate
from agentkit.agents.memory.maintenance.vitality import (
    is_protected,
    payload_of,
    vitality_score,
)
from agentkit.agents.memory.types import (
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemoryScope,
    SemanticPayload,
    utc_now,
)
from agentkit.config.memory_config import MemoryMaintenanceConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MaintenanceRunResult:
    run_id: str
    dry_run: bool
    archived: int = 0
    folded: int = 0
    errors: list[str] = field(default_factory=list)
    scanned_users: int = 0
    shadowed: int = 0


class MemoryMaintenanceService:
    """Archive low-vitality facts, then fold topic fragments into dense facts.

    One instance is owned by the gateway lifespan. run_once is synchronous and
    is called through asyncio.to_thread from the admin route.
    """

    def __init__(
        self,
        manager: MemoryManager,
        *,
        config: MemoryMaintenanceConfig | None = None,
        summarizer: Callable[[list[str]], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manager = manager
        self.config = config or manager.config.maintenance
        self.summarizer = summarizer
        self.clock = clock or utc_now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.config.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="memory-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            delay = self.config.interval_hours * 3600.0
            jitter = self.config.jitter_minutes * 60.0
            if jitter > 0:
                delay += random.uniform(0, jitter)
            if self._stop.wait(delay):
                return
            try:
                self.run_once(dry_run=None)
            except Exception:
                logger.exception("Scheduled memory maintenance failed")

    def run_once(
        self,
        *,
        dry_run: bool | None = None,
        shadow: bool | None = None,
        max_users: int | None = None,
        force: bool = False,
        scope: MemoryScope | None = None,
    ) -> MaintenanceRunResult:
        effective_dry = self.config.shadow if dry_run is None else bool(dry_run)
        effective_shadow = self.config.shadow if shadow is None else bool(shadow)
        run_id = f"maintenance_{int(time.time())}"
        result = MaintenanceRunResult(run_id=run_id, dry_run=effective_dry or effective_shadow)
        deadline = time.monotonic() + self.config.max_duration_seconds
        scopes = [scope] if scope is not None else self._scopes(max_users or self.config.max_users)
        for current in scopes:
            if time.monotonic() >= deadline:
                break
            result.scanned_users += 1
            try:
                self._run_scope(current, result, dry_run=effective_dry or effective_shadow, force=force)
            except Exception as exc:
                logger.exception("Memory maintenance failed for user %s", current.user_id)
                result.errors.append(f"{current.user_id}: {type(exc).__name__}")
        return result

    def _scopes(self, limit: int) -> list[MemoryScope]:
        page = self.manager.list_scopes(limit=min(limit, 1000))
        return page.items

    def _run_scope(
        self,
        scope: MemoryScope,
        result: MaintenanceRunResult,
        *,
        dry_run: bool,
        force: bool,
    ) -> None:
        with self.manager.lock_for(scope.user_id):
            now = self.clock()
            stats = self.manager.get_stats(scope)
            page = self.manager.list_records(
                scope, kind=MemoryKind.SEMANTIC, include_history=False, limit=100
            )
            active = [hit.record for hit in page.items if hit.record.status == "active"]
            over_watermark = len(active) > self.config.high_watermark
            access_starved = (
                self.config.min_search_calls_per_window > 0
                and stats.search_calls < self.config.min_search_calls_per_window
            )

            if not dry_run:
                self._purge_expired_archives(scope, now)
            archived = 0
            low_cycles = dict(stats.low_vitality_cycles or {})
            if (over_watermark or force) and not access_starved:
                candidates = []
                for record in active:
                    payload = payload_of(record)
                    score = vitality_score(record, now, payload=payload)
                    candidate = archive_candidate(
                        record,
                        now=now,
                        config=self.config,
                        vitality=score,
                        payload=payload,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                    if score < self.config.min_vitality and not is_protected(record, payload):
                        low_cycles[record.id] = low_cycles.get(record.id, 0) + 1
                    else:
                        low_cycles.pop(record.id, None)
                candidates.sort(key=lambda item: (item.vitality, item.record.id))
                max_by_ratio = max(1, int(len(active) * self.config.max_eviction_ratio))
                to_target = max(0, len(active) - self.config.target_count)
                budget = min(self.config.max_evictions_per_run, max_by_ratio, to_target)
                for candidate in candidates[:budget]:
                    payload = candidate.payload
                    if payload.source == "user_explicit":
                        if low_cycles.get(candidate.record.id, 0) < self.config.explicit_consecutive_low_cycles:
                            continue
                    if dry_run:
                        result.shadowed += 1
                        continue
                    archived_record = candidate.record.model_copy(
                        update={
                            "status": "archived",
                            "archived_at": now,
                            "metadata": {
                                **candidate.record.metadata,
                                "archive_reason": candidate.reason,
                                "vitality_at_archive": candidate.vitality,
                            },
                        }
                    )
                    self.manager.backend.put_record(
                        scope, archived_record, expected_revision=candidate.record.revision
                    )
                    archived += 1
            result.archived += archived

            folded = 0
            cooling_down = (
                stats.last_fold_at is not None
                and (now - stats.last_fold_at).total_seconds()
                < self.config.folding.cooldown_days * 86_400
            )
            if self.summarizer is not None and self.config.folding.enabled and not cooling_down:
                active_page = self.manager.list_records(
                    scope, kind=MemoryKind.SEMANTIC, include_history=False, limit=100
                )
                active = [hit.record for hit in active_page.items if hit.record.status == "active"]
                clusters = candidate_clusters(active, config=self.config.folding, now=now)
                for cluster in clusters[: self.config.folding.max_clusters_per_run]:
                    result_cluster = fold_cluster(
                        cluster,
                        summarizer=self.summarizer,
                        config=self.config.folding,
                        now=now,
                    )
                    if result_cluster is None:
                        continue
                    if dry_run:
                        result.shadowed += 1
                        continue
                    folded += self._apply_fold(scope, result_cluster, now=now)
            result.folded += folded

            if not dry_run:
                fields: dict[str, Any] = {
                    "last_maintenance_at": now,
                    "dirty": False,
                    "low_vitality_cycles": low_cycles,
                }
                if archived:
                    fields["last_eviction_at"] = now
                if folded:
                    fields["last_fold_at"] = now
                if hasattr(self.manager.backend, "update_stats"):
                    self.manager.backend.update_stats(scope, **fields)  # type: ignore[attr-defined]

    def _purge_expired_archives(self, scope: MemoryScope, now: datetime) -> int:
        """Physical delete after the 30-day recovery window. Best effort per row."""
        from datetime import timedelta

        page = self.manager.backend.search_records(
            scope,
            MemoryQuery(
                kinds=[MemoryKind.SEMANTIC],
                filters={"status": "archived"},
                include_history=True,
                limit=100,
                sort="recency",
            ),
        )
        cutoff = now - timedelta(days=self.config.archive_retention_days)
        expired = [
            hit.record.id
            for hit in page.items
            if hit.record.archived_at is not None and hit.record.archived_at <= cutoff
        ]
        if not expired:
            return 0
        return self.manager.backend.delete_records(
            scope, expired, mode="hard", reason="retention_expired"
        )

    def _apply_fold(self, scope: MemoryScope, result: FoldResult, *, now: datetime) -> int:
        record = build_folded_record(scope, result, now=now)
        # Folding is a write like any other: it must pass the semantic conflict strategy
        # before the consolidated fact becomes active.
        semantic = self.manager.service(MemoryKind.SEMANTIC)
        first = result.cluster.records[0]
        payload = SemanticPayload.model_validate(record.structured)
        resolution = semantic.resolver.resolve(
            MemoryKind.SEMANTIC,
            scope,
            first,
            MemoryProposal(
                id="fold-proposal",
                kind=MemoryKind.SEMANTIC,
                payload=payload.model_dump(mode="json"),
                evidence="semantic_fold",
                source="conversation",
            ),
        )
        if resolution.action in {"reject", "conflict"}:
            logger.info("Folded fact skipped by resolver: %s", resolution.reason)
            return 0
        existing = self.manager.backend.get_record(scope, record.kind, record.id)
        if existing is not None:
            return 0
        try:
            self.manager.backend.put_record(scope, record)
        except Exception:
            logger.warning("Folded fact write failed for user %s", scope.user_id, exc_info=True)
            return 0
        try:
            for original in result.cluster.records:
                updated = original.model_copy(
                    update={"status": "folded", "superseded_by": record.id}
                )
                self.manager.backend.put_record(
                    scope, updated, expected_revision=original.revision
                )
        except Exception:
            # Never leave a half merge. Deleting the new fact keeps originals canonical.
            logger.warning(
                "Folding originals failed; rolling back the consolidated fact",
                exc_info=True,
            )
            self.manager.backend.delete_records(
                scope, [record.id], mode="hard", reason="fold_rollback"
            )
            return 0
        return 1
