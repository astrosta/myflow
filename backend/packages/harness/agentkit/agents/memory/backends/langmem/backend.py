"""LangGraph BaseStore adapter (M10 section 2.5).

LangMem is an extraction layer, not a store. This adapter is the generic
store-side MemoryBackend the manager and every type service talk to.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.types import (
    MemoryBackendCapabilities,
    MemoryCorruptionError,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    MemoryStats,
    MemoryConflictError,
    ScopePage,
    ensure_utc,
    utc_now,
)

logger = logging.getLogger(__name__)

PROFILE_NAMESPACE = ("users", "{user_id}", "profile")
RECORDS_NAMESPACE = ("users", "{user_id}", "records")
STATS_NAMESPACE = ("users", "{user_id}", "stats")
PROFILE_KEY = "current"
STATS_KEY = "current"
STATS_ITEM_KIND = "__stats__"
_SCAN_LIMIT = 10_000
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


def _namespace(template: tuple[str, ...], user_id: str) -> tuple[str, ...]:
    return tuple(part.format(user_id=user_id) for part in template)


def _record_key(record: MemoryRecord) -> str:
    return f"{record.kind}:{record.id}"


def _safe_text(value: str) -> str:
    return value.replace("\x00", "")


class LangMemBackend(MemoryBackend):
    """MemoryBackend over a LangGraph BaseStore.

    The store owns durability (memory / sqlite / postgres). This class owns the
    user namespace, record envelope serialization, metadata filtering, and the
    in-process CAS guard. BaseStore itself is last-write-wins; cross-process CAS
    is a deployment concern documented in the design, not silently claimed.
    """

    capabilities: ClassVar[MemoryBackendCapabilities] = MemoryBackendCapabilities(
        vector_search=True,
        metadata_filter=True,
        batch_write=True,
        cas=True,
        list_scopes=True,
        ttl=False,
    )

    def __init__(self, store: Any, *, backend_config: dict[str, Any] | None = None, callbacks: Any = None) -> None:
        if store is None:
            raise ValueError("LangMemBackend requires a LangGraph BaseStore")
        self._store = store
        self._config = dict(backend_config or {})
        self._callbacks = callbacks
        self._lock = threading.RLock()
        self._owns_store = False

    @classmethod
    def from_config(
        cls,
        backend_config: dict[str, Any],
        *,
        store: Any,
        callbacks: Any = None,
    ) -> "LangMemBackend":
        return cls(store, backend_config=backend_config, callbacks=callbacks)

    # -- record I/O ---------------------------------------------------------

    def get_record(self, scope: MemoryScope, kind: str, record_id: str) -> MemoryRecord | None:
        if kind == MemoryKind.PROFILE:
            item = self._store.get(_namespace(PROFILE_NAMESPACE, scope.user_id), PROFILE_KEY)
        else:
            item = self._store.get(
                _namespace(RECORDS_NAMESPACE, scope.user_id), f"{kind}:{record_id}"
            )
        return self._decode(item) if item is not None else None

    def put_record(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        *,
        expected_revision: int | None = None,
    ) -> MemoryRecord:
        if record.user_id != scope.user_id:
            raise ValueError("record.user_id must match the write scope")
        with self._lock:
            current = self.get_record(scope, record.kind, record.id)
            current_revision = current.revision if current is not None else None
            if expected_revision is not None and current_revision != expected_revision:
                raise MemoryConflictError(
                    f"memory record {record.kind}/{record.id} changed (expected revision "
                    f"{expected_revision}, found {current_revision})",
                    current_revision=current_revision,
                )
            revision = record.revision
            if current is not None:
                revision = max(revision, current.revision + 1)
            stored = record.model_copy(
                update={
                    "revision": revision,
                    "updated_at": utc_now(),
                    "content": _safe_text(record.content),
                    "search_text": _safe_text(record.search_text) if record.search_text else record.search_text,
                }
            )
            self._put_stored(scope, stored)
            return stored

    def _put_stored(self, scope: MemoryScope, record: MemoryRecord) -> None:
        value = record.model_dump(mode="json")
        if record.kind == MemoryKind.PROFILE:
            self._store.put(
                _namespace(PROFILE_NAMESPACE, scope.user_id), PROFILE_KEY, value, index=False
            )
            return
        # Only searchable records are embedded. Profile, stats, tombstones, and audit
        # rows must never consume vector index entries.
        vector = (
            str(self._config.get("retrieval_adapter", "lexical")).lower() == "vector"
            and record.kind not in {"tombstone", "instruction", "conflict", "stats"}
        )
        index = list(self._config.get("index_fields") or ["search_text"]) if vector else False
        self._store.put(
            _namespace(RECORDS_NAMESPACE, scope.user_id),
            _record_key(record),
            value,
            index=index,
        )

    def _decode(self, item: Any) -> MemoryRecord:
        try:
            return MemoryRecord.model_validate(item.value)
        except Exception as exc:
            raise MemoryCorruptionError(
                f"stored memory record {getattr(item, 'key', '<unknown>')} is corrupt"
            ) from exc

    # -- search -------------------------------------------------------------

    def search_records(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        vector_hits = self._vector_hits(scope, query)
        if vector_hits is not None:
            filtered = [
                (record, score)
                for record, score in vector_hits
                if self._matches(record, query, check_text=False)
            ]
            scored: list[tuple[MemoryRecord, float | None]] = list(filtered)
        else:
            records = self._all_records(scope, include_profile=False)
            filtered = [
                record
                for record in records
                if self._matches(record, query)
            ]
            if query.text:
                lexical = [(record, self._score(record, query.text)) for record in filtered]
                scored = [(record, score) for record, score in lexical if score > 0.0]
            else:
                scored = [(record, None) for record in filtered]
        now = utc_now()

        if query.sort == "relevance" and query.text:
            scored.sort(key=lambda pair: (-(pair[1] or 0.0), _desc_time(pair[0].updated_at), pair[0].id))
        elif query.sort == "vitality":
            scored.sort(key=lambda pair: (_vitality_proxy(pair[0], now), pair[0].id))
        else:
            scored.sort(key=lambda pair: (_desc_time(pair[0].updated_at), pair[0].id))

        offset = _decode_cursor(query.cursor)
        page = scored[offset : offset + query.limit]
        next_cursor = str(offset + query.limit) if offset + query.limit < len(scored) else None
        items = [
            {
                "record": record,
                "score": score,
                "highlights": _highlights(record, query.text),
            }
            for record, score in page
        ]
        return MemorySearchPage.model_validate({"items": items, "next_cursor": next_cursor})

    def _vector_hits(
        self, scope: MemoryScope, query: MemoryQuery
    ) -> list[tuple[MemoryRecord, float | None]] | None:
        """Try the store's vector index. None means fall back to the lexical scan.

        The metadata filter is pushed into the store so active/kind/agent/topic rows are
        removed before top-k, not after it. A store without a configured index raises,
        and lexical retrieval is the documented degradation path.
        """
        if not query.text or str(self._config.get("retrieval_adapter", "lexical")).lower() != "vector":
            return None
        store_filter: dict[str, Any] = {}
        if query.kinds and len(query.kinds) == 1:
            store_filter["kind"] = query.kinds[0]
        if not query.include_history:
            store_filter["status"] = "active"
        for key in ("topic_key", "agent_name", "source"):
            value = query.filters.get(key)
            if isinstance(value, str):
                store_filter[key] = value
        try:
            items = self._store.search(
                _namespace(RECORDS_NAMESPACE, scope.user_id),
                query=query.text,
                filter=store_filter or None,
                limit=min(_SCAN_LIMIT, max(50, query.limit * 4)),
            )
        except Exception:
            logger.debug("Vector search unavailable; using lexical fallback", exc_info=True)
            return None
        return [(self._decode(item), item.score) for item in items]

    def _all_records(self, scope: MemoryScope, *, include_profile: bool = False) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        if include_profile:
            profile = self.get_record(scope, MemoryKind.PROFILE, "profile")
            if profile is not None:
                records.append(profile)
        items = self._store.search(
            _namespace(RECORDS_NAMESPACE, scope.user_id), limit=_SCAN_LIMIT
        )
        records.extend(self._decode(item) for item in items)
        return records

    def _matches(
        self, record: MemoryRecord, query: MemoryQuery, *, check_text: bool = True
    ) -> bool:
        if query.kinds is not None and record.kind not in query.kinds:
            return False
        if not query.include_history and record.status != "active":
            return False
        for key, expected in query.filters.items():
            actual: Any
            if key == "status":
                actual = record.status
            elif key == "topic_key":
                actual = record.topic_key
            elif key == "source":
                actual = record.source
            elif key == "agent_name":
                actual = record.agent_name
            elif key == "category":
                actual = record.structured.get("category")
            elif key == "tags":
                actual = record.tags
            else:
                actual = record.metadata.get(key)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        if query.time_range is not None:
            start, end = query.time_range
            start = ensure_utc(start)
            end = ensure_utc(end)
            anchor = record.valid_from or record.created_at
            if not (start <= anchor <= end):
                return False
        if check_text and query.text and not self._score(record, query.text):
            return False
        return True

    def _score(self, record: MemoryRecord, text: str) -> float:
        needle = text.strip().lower()
        if not needle:
            return 0.0
        haystack = (record.search_text or record.content or "").lower()
        if not haystack:
            return 0.0
        if needle in haystack:
            return 1.0
        needle_tokens = set(_TOKEN_RE.findall(needle))
        hay_tokens = set(_TOKEN_RE.findall(haystack))
        if not needle_tokens:
            return 0.0
        overlap = len(needle_tokens & hay_tokens) / len(needle_tokens)
        return round(overlap * 0.5, 6)

    # -- delete / stats / scopes -------------------------------------------

    def delete_records(
        self,
        scope: MemoryScope,
        record_ids: list[str],
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str,
    ) -> int:
        targets = set(record_ids)
        changed = 0
        with self._lock:
            for record in self._all_records(scope, include_profile=False):
                if record.id not in targets:
                    continue
                if mode == "hard":
                    self._store.delete(
                        _namespace(RECORDS_NAMESPACE, scope.user_id), _record_key(record)
                    )
                else:
                    now = utc_now()
                    stored = record.model_copy(
                        update={
                            "status": "deleted",
                            "deleted_at": now,
                            "archived_at": now,
                            "metadata": {**record.metadata, "delete_reason": reason},
                        }
                    )
                    self._put_stored(scope, stored)
                changed += 1
        return changed

    def delete_profile(self, scope: MemoryScope) -> None:
        with self._lock:
            self._store.delete(_namespace(PROFILE_NAMESPACE, scope.user_id), PROFILE_KEY)

    def get_stats(self, scope: MemoryScope, *, kind: str | None = None) -> MemoryStats:
        counts: dict[str, int] = {}
        for record in self._all_records(scope, include_profile=True):
            if kind is not None and record.kind != kind:
                continue
            if record.status == "deleted":
                continue
            counts[record.kind] = counts.get(record.kind, 0) + 1
        stored = self._read_stats(scope)
        stored.counts_by_kind = counts
        stored.user_id = scope.user_id
        return stored

    def _read_stats(self, scope: MemoryScope) -> MemoryStats:
        try:
            item = self._store.get(_namespace(STATS_NAMESPACE, scope.user_id), STATS_KEY)
        except Exception:
            logger.warning("Memory stats read failed", exc_info=True)
            return MemoryStats(user_id=scope.user_id)
        if item is None:
            return MemoryStats(user_id=scope.user_id)
        try:
            return MemoryStats.model_validate(item.value)
        except Exception:
            logger.warning("Memory stats are corrupt; starting from an empty projection")
            return MemoryStats(user_id=scope.user_id)

    def note_search(self, scope: MemoryScope) -> None:
        with self._lock:
            stats = self._read_stats(scope)
            self._store.put(
                _namespace(STATS_NAMESPACE, scope.user_id),
                STATS_KEY,
                stats.model_copy(update={"search_calls": stats.search_calls + 1}).model_dump(mode="json"),
                index=False,
            )

    def update_stats(self, scope: MemoryScope, **fields: Any) -> MemoryStats:
        with self._lock:
            stats = self._read_stats(scope)
            updated = stats.model_copy(update=fields)
            self._store.put(
                _namespace(STATS_NAMESPACE, scope.user_id),
                STATS_KEY,
                updated.model_dump(mode="json"),
                index=False,
            )
            return updated

    def list_scopes(self, *, cursor: str | None = None, limit: int = 100) -> ScopePage:
        limit = max(1, min(limit, 1000))
        try:
            namespaces = self._store.list_namespaces(prefix=("users",), max_depth=2, limit=1000)
        except Exception:
            logger.warning("Memory scope listing failed", exc_info=True)
            namespaces = []
        user_ids = sorted(
            namespace[1]
            for namespace in namespaces
            if len(namespace) == 2 and namespace[0] == "users"
        )
        offset = _decode_cursor(cursor)
        page = user_ids[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(user_ids) else None
        return ScopePage(
            items=[MemoryScope(user_id=user_id) for user_id in page],
            next_cursor=next_cursor,
        )

    def close(self) -> None:
        close = getattr(self._store, "close", None)
        if callable(close):
            close()


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid memory cursor") from exc
    if value < 0:
        raise ValueError("invalid memory cursor")
    return value


def _desc_time(value: datetime) -> float:
    return -ensure_utc(value).timestamp()


def _vitality_proxy(record: MemoryRecord, now: datetime) -> float:
    age = max(0.0, (now - ensure_utc(record.created_at)).total_seconds() / 86_400)
    return record.confidence * math.exp(-age / 30.0)


def _highlights(record: MemoryRecord, query: str | None) -> list[str]:
    if not query:
        return []
    content = record.content or ""
    needle = query.strip().lower()
    index = content.lower().find(needle)
    if index < 0:
        return []
    start = max(0, index - 60)
    return [content[start : start + 180]]
