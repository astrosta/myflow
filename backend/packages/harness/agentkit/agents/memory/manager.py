"""MemoryManager composition root (M10 section 2.2.5 / 2.6)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.ingestors.base import MemoryIngestor
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.agents.memory.services.episodic import EpisodicMemoryService
from agentkit.agents.memory.services.procedural import ProceduralMemoryService
from agentkit.agents.memory.services.profile import ProfileService
from agentkit.agents.memory.services.semantic import SemanticMemoryService
from agentkit.agents.memory.types import (
    MemoryConflict,
    MemoryExport,
    MemoryKind,
    MemoryKindNotSupported,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    MemoryStats,
    ScopePage,
    SemanticPayload,
    utc_now,
)
from agentkit.config.memory_config import MemoryConfig
from agentkit.privacy import PIIRedactor, has_redaction_marker, memory_view_messages

logger = logging.getLogger(__name__)

CONFLICT_KIND = "conflict"
_BACKEND_ALIASES = {
    "langmem": "agentkit.agents.memory.backends.langmem.backend:LangMemBackend",
    "noop": "agentkit.agents.memory.backends.noop.backend:NoopMemoryBackend",
}


@dataclass
class _PendingBatch:
    epoch: int
    scope: MemoryScope
    messages: list[Any] = field(default_factory=list)


class MemoryManager:
    """Facade that routes by kind. It is not a backend base class."""

    def __init__(
        self,
        backend: MemoryBackend,
        services: list[MemoryTypeService] | None = None,
        ingestors: list[MemoryIngestor] | None = None,
        *,
        config: MemoryConfig | None = None,
        callbacks: Any = None,
    ) -> None:
        self.backend = backend
        self.ingestors = list(ingestors or [])
        self.config = config or MemoryConfig()
        self._callbacks = callbacks
        self._services: dict[str, MemoryTypeService] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._pending: dict[tuple[str, str | None], _PendingBatch] = {}
        self._timers: dict[tuple[str, str | None], threading.Timer] = {}
        self._epochs: dict[str, int] = {}
        self._condition = threading.Condition(threading.RLock())
        self._active_workers = 0
        self._closed = False
        for service in services or []:
            self.register(service)

    @classmethod
    def from_config(
        cls,
        backend: MemoryBackend,
        services: list[MemoryTypeService],
        ingestors: list[MemoryIngestor],
        *,
        callbacks: Any = None,
        config: MemoryConfig | None = None,
    ) -> "MemoryManager":
        return cls(backend, services, ingestors, callbacks=callbacks, config=config)

    def register(self, service: MemoryTypeService) -> None:
        if service.kind != service.spec.kind:
            raise ValueError(
                f"service kind {service.kind!r} does not match its spec {service.spec.kind!r}"
            )
        if service.kind in self._services:
            raise ValueError(f"memory kind {service.kind!r} is already registered")
        if service.spec.searchable and not self.backend.capabilities.metadata_filter:
            raise ValueError(
                f"backend {type(self.backend).__name__} cannot register searchable kind "
                f"{service.kind!r}: metadata filtering is required before top-k"
            )
        self._services[service.kind] = service

    def service(self, kind: str) -> MemoryTypeService:
        try:
            return self._services[kind]
        except KeyError as exc:
            raise MemoryKindNotSupported(f"memory kind {kind!r} is not registered") from exc

    @property
    def services(self) -> dict[str, MemoryTypeService]:
        """Read-only view for hosts that need inventory; mutations go through register()."""
        return dict(self._services)

    @property
    def supported_kinds(self) -> set[str]:
        return set(self._services)

    def lock_for(self, user_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.RLock())

    # -- reads --------------------------------------------------------------

    def get_record(self, scope: MemoryScope, kind: str, record_id: str) -> MemoryRecord | None:
        return self.service(kind).get(scope, record_id)

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        requested = query.kinds
        if requested is None:
            kinds = [
                kind
                for kind, service in self._services.items()
                if service.spec.searchable
            ]
        else:
            for kind in requested:
                self.service(kind)
            kinds = [kind for kind in requested if self.service(kind).spec.searchable]
        if not kinds:
            return MemorySearchPage()
        if len(kinds) == 1:
            return self.service(kinds[0]).search(
                scope, query.model_copy(update={"kinds": kinds})
            )
        page = self.backend.search_records(scope, query.model_copy(update={"kinds": kinds}))
        # Cross-kind search goes straight to the backend for one ranking pass, but
        # semantic-specific visibility rules still belong to its service.
        visible = []
        for hit in page.items:
            if hit.record.kind == MemoryKind.SEMANTIC:
                try:
                    payload = SemanticPayload.model_validate(hit.record.structured)
                except Exception:
                    continue
                if payload.profile_linked or payload.valid_to is not None:
                    continue
            visible.append(hit)
        return MemorySearchPage(items=visible, next_cursor=page.next_cursor)

    def list_records(
        self,
        scope: MemoryScope,
        *,
        kind: str,
        filters: dict[str, Any] | None = None,
        include_history: bool = True,
        limit: int = 50,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        self.service(kind)
        return self.backend.search_records(
            scope,
            MemoryQuery(
                kinds=[kind],
                filters=filters or {},
                limit=limit,
                cursor=cursor,
                include_history=include_history,
                sort="recency",
            ),
        )

    def clear_profile_field(self, scope: MemoryScope, field_path: str) -> None:
        with self.lock_for(scope.user_id):
            self.service(MemoryKind.PROFILE).clear(scope, field_path=field_path)  # type: ignore[attr-defined]
            semantic = self._services.get(MemoryKind.SEMANTIC)
            if semantic is not None and hasattr(semantic, "invalidate_profile_field"):
                semantic.invalidate_profile_field(scope, field_path)  # type: ignore[attr-defined]

    def delete_record(
        self,
        scope: MemoryScope,
        kind: str,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str = "user_forget",
    ) -> None:
        with self.lock_for(scope.user_id):
            self.service(kind).delete(scope, record_id, mode=mode, reason=reason)

    def set_explicit(
        self, scope: MemoryScope, kind: str, record_id: str, explicit: bool
    ) -> MemoryRecord | None:
        service = self.service(kind)
        method = getattr(service, "set_explicit", None)
        if not callable(method):
            raise MemoryKindNotSupported(f"memory kind {kind!r} does not support explicit_remember")
        with self.lock_for(scope.user_id):
            return method(scope, record_id, explicit)

    # -- ingest -------------------------------------------------------------

    def ingest(self, scope: MemoryScope, messages: list[Any], *, nowait: bool = False) -> None:
        """Enqueue a debounced extraction batch. This method never calls a model inline."""
        del nowait  # the queue is always non-blocking; ingest_now is the explicit sync path
        if not self.ingestors or not messages:
            return
        # Memory is a long-lived consumer: it never sees handles or literal PII.
        messages = memory_view_messages(messages)
        key = (scope.user_id, scope.thread_id)
        with self._condition:
            if self._closed:
                return
            epoch = self._epochs.get(scope.user_id, 0)
            pending = self._pending.get(key)
            if pending is None or pending.epoch != epoch:
                pending = _PendingBatch(epoch=epoch, scope=scope)
                self._pending[key] = pending
            pending.messages.extend(messages[-20:])
            timer = self._timers.pop(key, None)
            if timer is not None:
                timer.cancel()
            delay = max(0.0, float(self.config.ingest.debounce_seconds))
            timer = threading.Timer(delay, self._drain, args=(key, epoch))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def ingest_now(self, scope: MemoryScope, messages: list[Any]) -> list[MemoryRecord]:
        """Extraction + routing without debounce. Used by tests and explicit flushes."""
        if not self.ingestors or not messages:
            return []
        return self._process(scope, memory_view_messages(messages))

    def _drain(self, key: tuple[str, str | None], epoch: int) -> None:
        with self._condition:
            pending = self._pending.pop(key, None)
            self._timers.pop(key, None)
            if pending is None or pending.epoch != epoch:
                self._condition.notify_all()
                return
            self._active_workers += 1
        try:
            self._process(pending.scope, pending.messages, epoch=epoch)
        finally:
            with self._condition:
                self._active_workers -= 1
                self._condition.notify_all()

    def _process(
        self, scope: MemoryScope, messages: list[Any], *, epoch: int | None = None
    ) -> list[MemoryRecord]:
        if self._closed:
            return []
        expected_epoch = self._epochs.get(scope.user_id, 0) if epoch is None else epoch
        proposals: list[MemoryProposal] = []
        for ingestor in self.ingestors:
            try:
                proposals.extend(ingestor.extract(scope, messages))
            except Exception:
                logger.warning(
                    "Memory ingestor %s failed; dropping its proposals",
                    type(ingestor).__name__,
                    exc_info=True,
                )
        proposals = [
            scrubbed
            for proposal in proposals
            if (scrubbed := self._scrub_proposal(proposal)) is not None
        ]
        if not proposals:
            return []
        if self._epochs.get(scope.user_id, 0) != expected_epoch:
            logger.info("Dropping stale memory proposals for user %s (scope was cleared)", scope.user_id)
            return []
        grouped: dict[str, list[MemoryProposal]] = {}
        for proposal in proposals:
            if proposal.kind not in self._services:
                logger.warning("Dropping proposal for unregistered memory kind %r", proposal.kind)
                continue
            grouped.setdefault(proposal.kind, []).append(proposal)
        written: list[MemoryRecord] = []
        with self.lock_for(scope.user_id):
            if self._epochs.get(scope.user_id, 0) != expected_epoch:
                logger.info("Dropping stale memory proposals for user %s before write", scope.user_id)
                return []
            for kind, batch in grouped.items():
                try:
                    written.extend(self.service(kind).write(scope, batch))
                except ValueError:
                    logger.warning("Memory proposal batch for %s was invalid; failing closed", kind, exc_info=True)
                except Exception:
                    logger.exception("Memory write failed for kind %s", kind)
        self._emit(
            {
                "type": "memory.ingest",
                "user_id": scope.user_id,
                "thread_id": scope.thread_id,
                "written": len(written),
            }
        )
        return written

    def _scrub_proposal(self, proposal: MemoryProposal) -> MemoryProposal | None:
        """Defense in depth: a model must not smuggle a handle or literal PII into memory."""
        redactor = PIIRedactor()
        payload = dict(proposal.payload)
        for key, value in list(payload.items()):
            cleaned = _memory_clean_value(value, redactor)
            if cleaned is _DROP:
                logger.info("Dropping a memory proposal that contains a redaction marker")
                return None
            payload[key] = cleaned
        evidence = redactor.redact_for_memory(proposal.evidence)
        if has_redaction_marker(evidence):
            evidence = ""
        return proposal.model_copy(update={"payload": payload, "evidence": evidence})

    def _emit(self, event: dict[str, Any]) -> None:
        callback = self._callbacks
        if callback is None:
            return
        try:
            if callable(callback):
                callback(event)
            elif hasattr(callback, "on_memory_llm_result"):
                callback.on_memory_llm_result(event)
        except Exception:
            logger.warning("Memory callback failed", exc_info=True)

    def cancel_pending(self, scope: MemoryScope, *, epoch: int) -> int:
        removed = 0
        with self._condition:
            for key, pending in list(self._pending.items()):
                if key[0] != scope.user_id or pending.epoch > epoch:
                    continue
                self._pending.pop(key, None)
                timer = self._timers.pop(key, None)
                if timer is not None:
                    timer.cancel()
                removed += 1
            self._condition.notify_all()
        return removed

    def flush(self, timeout: float = 30.0) -> bool:
        """Drain queued batches and wait for active workers within a hard budget."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            pending_items = list(self._pending.items())
            self._pending.clear()
            for key, timer in list(self._timers.items()):
                timer.cancel()
            self._timers.clear()
        for _key, pending in pending_items:
            if self._epochs.get(pending.scope.user_id, 0) != pending.epoch:
                continue
            with self._condition:
                self._active_workers += 1
            try:
                self._process(pending.scope, pending.messages)
            finally:
                with self._condition:
                    self._active_workers -= 1
                    self._condition.notify_all()
        with self._condition:
            while self._active_workers > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def clear(self, scope: MemoryScope, *, kinds: list[str] | None = None) -> None:
        target_kinds = set(kinds) if kinds is not None else None
        with self._condition:
            old_epoch = self._epochs.get(scope.user_id, 0)
            self._epochs[scope.user_id] = old_epoch + 1
        self.cancel_pending(scope, epoch=old_epoch)
        with self.lock_for(scope.user_id):
            if target_kinds is None or MemoryKind.PROFILE in target_kinds:
                try:
                    self.backend.delete_profile(scope)
                except NotImplementedError:
                    self.service(MemoryKind.PROFILE).delete(scope, "profile", mode="hard")
                except Exception:
                    logger.warning("Profile clear failed", exc_info=True)
            page = self.backend.search_records(
                scope,
                MemoryQuery(include_history=True, limit=100, sort="recency"),
            )
            def _clearable(record: MemoryRecord) -> bool:
                if target_kinds is not None:
                    return record.kind in target_kinds or record.kind == CONFLICT_KIND
                # Tombstones and instruction audits survive a clear; account deletion is
                # the only path allowed to erase them.
                return record.kind not in {"tombstone", "instruction"}

            ids = [hit.record.id for hit in page.items if _clearable(hit.record)]
            # Iterate until the store has no more matching rows.
            while page.next_cursor:
                page = self.backend.search_records(
                    scope,
                    MemoryQuery(
                        include_history=True,
                        limit=100,
                        cursor=page.next_cursor,
                        sort="recency",
                    ),
                )
                ids.extend(hit.record.id for hit in page.items if _clearable(hit.record))
            if ids:
                self.backend.delete_records(scope, ids, mode="hard", reason="user_clear")

    def export(self, scope: MemoryScope, *, kinds: list[str] | None = None) -> MemoryExport:
        profile_record = self.backend.get_record(scope, MemoryKind.PROFILE, "profile")
        record_ids = set(kinds) if kinds is not None else None
        records: list[MemoryRecord] = []
        page = self.backend.search_records(
            scope, MemoryQuery(include_history=True, limit=100, sort="recency")
        )
        while True:
            records.extend(
                hit.record
                for hit in page.items
                if hit.record.kind in self._services
                and (record_ids is None or hit.record.kind in record_ids)
            )
            if not page.next_cursor:
                break
            page = self.backend.search_records(
                scope,
                MemoryQuery(
                    include_history=True,
                    limit=100,
                    cursor=page.next_cursor,
                    sort="recency",
                ),
            )
        conflicts = self.list_conflicts(scope, include_resolved=True)
        return MemoryExport(
            profile=profile_record.structured if profile_record is not None else None,
            records=records,
            conflicts=conflicts,
            maintenance=self.get_stats(scope).model_dump(mode="json"),
        )

    def get_stats(self, scope: MemoryScope) -> MemoryStats:
        return self.backend.get_stats(scope)

    def note_search(self, scope: MemoryScope) -> None:
        """Count one successful memory_search call for maintenance activity gating."""
        note = getattr(self.backend, "note_search", None)
        if callable(note):
            note(scope)

    def list_scopes(self, *, cursor: str | None = None, limit: int = 100) -> ScopePage:
        return self.backend.list_scopes(cursor=cursor, limit=limit)

    # -- conflicts ----------------------------------------------------------

    def list_conflicts(self, scope: MemoryScope, *, include_resolved: bool = False) -> list[MemoryConflict]:
        page = self.backend.search_records(
            scope,
            MemoryQuery(kinds=[CONFLICT_KIND], include_history=True, limit=100, sort="recency"),
        )
        conflicts: list[MemoryConflict] = []
        for hit in page.items:
            conflict = self._conflict_from_record(hit.record)
            if conflict is None:
                continue
            if include_resolved or conflict.status == "open":
                conflicts.append(conflict)
        return conflicts

    def resolve_conflict(
        self,
        scope: MemoryScope,
        conflict_id: str,
        resolution: Literal["current", "candidate", "merge"],
        *,
        merged_content: str | None = None,
    ) -> MemoryConflict:
        with self.lock_for(scope.user_id):
            return self._resolve_conflict(scope, conflict_id, resolution, merged_content=merged_content)

    def _resolve_conflict(
        self,
        scope: MemoryScope,
        conflict_id: str,
        resolution: Literal["current", "candidate", "merge"],
        *,
        merged_content: str | None = None,
    ) -> MemoryConflict:
        conflict_record = self.backend.get_record(scope, CONFLICT_KIND, conflict_id)
        if conflict_record is None:
            raise KeyError(conflict_id)
        conflict = self._conflict_from_record(conflict_record)
        if conflict is None:
            raise ValueError("stored conflict is corrupt")
        current = (
            self.backend.get_record(scope, conflict.kind, conflict.current_record_id)
            if conflict.current_record_id
            else None
        )
        candidate = (
            self.backend.get_record(scope, conflict.kind, conflict.candidate_record_id)
            if conflict.candidate_record_id
            else None
        )
        if resolution == "current":
            if candidate is not None:
                self.backend.delete_records(scope, [candidate.id], mode="soft", reason="conflict_rejected")
        elif candidate is not None:
            if merged_content:
                merged_content = PIIRedactor().redact_for_memory(merged_content)
                if has_redaction_marker(merged_content):
                    raise ValueError("merged content must not contain redaction markers")
            if current is not None:
                superseded = current.model_copy(update={"status": "superseded", "superseded_by": candidate.id})
                self.backend.put_record(scope, superseded, expected_revision=current.revision)
            updates: dict[str, Any] = {"status": "active"}
            if resolution == "merge" and merged_content:
                updates["content"] = merged_content
                updates["search_text"] = merged_content
                structured = dict(candidate.structured)
                structured["content"] = merged_content
                updates["structured"] = structured
            candidate = candidate.model_copy(update=updates)
            self.backend.put_record(scope, candidate, expected_revision=candidate.revision)
        resolved = conflict.model_copy(update={"status": "resolved"})
        self.backend.put_record(
            scope,
            conflict_record.model_copy(update={"structured": resolved.model_dump(mode="json")}),
            expected_revision=conflict_record.revision,
        )
        return resolved

    def _conflict_from_record(self, record: MemoryRecord) -> MemoryConflict | None:
        try:
            return MemoryConflict.model_validate(record.structured)
        except Exception:
            logger.warning("Ignoring corrupt conflict record %s", record.id)
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()
            self._condition.notify_all()
        self.backend.close()


# ---------------------------------------------------------------------------
# Process singleton / factory
# ---------------------------------------------------------------------------

_manager: MemoryManager | None = None
_manager_lock = threading.Lock()
_store: Any = None


def configure_memory_store(store: Any) -> None:
    """Inject the lifespan-owned LangGraph store before the first manager is built."""
    global _manager, _store
    with _manager_lock:
        if _manager is not None:
            _manager.close()
            _manager = None
        _store = store


def reset_memory_manager() -> None:
    global _manager, _store
    with _manager_lock:
        if _manager is not None:
            _manager.close()
        _manager = None
        _store = None


def get_memory_manager(*, store: Any = None, app_config: Any = None) -> MemoryManager:
    global _manager, _store
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is not None:
            return _manager
        config = _resolve_config(app_config)
        if store is None:
            store = _store
        if store is None:
            from langgraph.store.memory import InMemoryStore

            store = InMemoryStore()
            _store = store
        backend_class = _resolve_backend_class(config.backend_class)
        backend = backend_class.from_config(
            config.backend_config, store=store, callbacks=None
        )
        resolver = MemoryResolver()
        token_counting = str(config.backend_config.get("token_counting", "char"))
        services: list[MemoryTypeService] = [
            ProfileService(
                backend,
                resolver,
                max_tokens=config.profile.max_tokens,
                token_counting=token_counting,
            ),
            SemanticMemoryService(backend, resolver),
            EpisodicMemoryService(backend, resolver),
            ProceduralMemoryService(backend, resolver),
        ]
        ingestors: list[MemoryIngestor] = []
        try:
            from agentkit.agents.memory.ingestors.langmem_semantic import LangMemSemanticIngestor

            ingestor = LangMemSemanticIngestor.from_config(
                config.backend_config, app_config=app_config
            )
            if ingestor is not None:
                ingestors.append(ingestor)
        except Exception:
            logger.warning("Memory extraction is unavailable; facts extraction stays disabled", exc_info=True)
        semantic = next(service for service in services if service.kind == MemoryKind.SEMANTIC)
        semantic.ingestors = list(ingestors)
        _manager = MemoryManager(
            backend,
            services,
            ingestors,
            config=config,
            callbacks=None,
        )
        return _manager


_DROP = object()


def _memory_clean_value(value: Any, redactor: PIIRedactor) -> Any:
    if isinstance(value, str):
        cleaned = redactor.redact_for_memory(value)
        return _DROP if has_redaction_marker(cleaned) else cleaned
    if isinstance(value, list):
        result = []
        for item in value:
            cleaned = _memory_clean_value(item, redactor)
            if cleaned is _DROP:
                return _DROP
            result.append(cleaned)
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _memory_clean_value(item, redactor)
            if cleaned is _DROP:
                return _DROP
            result[key] = cleaned
        return result
    return value


def _resolve_config(app_config: Any) -> MemoryConfig:
    if app_config is not None and getattr(app_config, "memory", None) is not None:
        return app_config.memory
    if app_config is None:
        try:
            from agentkit.config import get_app_config

            loaded = get_app_config()
            if getattr(loaded, "memory", None) is not None:
                return loaded.memory
        except Exception:
            pass
    return MemoryConfig()


def _resolve_backend_class(path: str):
    from agentkit.agents.memory.backend import MemoryBackend
    from agentkit.reflection import resolve_variable

    resolved = _BACKEND_ALIASES.get(path, path)
    if ":" not in resolved:
        raise ValueError(
            f"unknown memory.backend_class {path!r}; expected one of "
            f"{sorted(_BACKEND_ALIASES)} or a module:Class path"
        )
    backend_class = resolve_variable(resolved, expected_type=type)
    if not isinstance(backend_class, type) or not issubclass(backend_class, MemoryBackend):
        raise ValueError(f"{path!r} is not a MemoryBackend subclass")
    return backend_class
