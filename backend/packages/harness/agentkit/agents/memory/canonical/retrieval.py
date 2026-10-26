"""Hybrid memory retrieval with PostgreSQL confirmation.

Milvus supplies only candidates.  Every candidate is re-read from PostgreSQL
and rejected unless it is active, belongs to the caller, is temporally valid,
and matches the projected revision.  If embedding/Milvus fails, the lexical
channel still answers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Sequence

from agentkit.agents.memory.canonical.domain import (
    MemoryHitView,
    MemoryRecordView,
    MemorySearchPage,
)
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)

_RRF_K = 60
_HALF_LIFE_DAYS = {"semantic": 90.0, "episodic": 7.0}


class HybridRetriever:
    def __init__(
        self,
        repo: MemoryRepository,
        projection: Any,
        embedder: Any,
        config: MemoryConfig,
    ) -> None:
        self._repo = repo
        self._projection = projection
        self._embedder = embedder
        self._config = config

    async def search(
        self,
        user_id: str,
        *,
        text: str,
        kinds: Sequence[str],
        limit: int = 8,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        selected = [kind for kind in kinds if kind in _HALF_LIFE_DAYS]
        if not selected:
            return MemorySearchPage()
        offset = _decode_cursor(cursor)
        candidate_k = int(self._config.retrieval.candidate_k)
        dense = await self._dense_candidates(user_id, text, selected, candidate_k)
        lexical = await self._repo.search_lexical(
            user_id, text=text, kinds=selected, limit=candidate_k
        )

        scores: dict[str, float] = {}
        revisions: dict[str, int] = {}
        for rank, hit in enumerate(dense):
            memory_id = hit["memory_id"]
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            revisions[memory_id] = int(hit.get("indexed_revision", -1))
        for rank, (record, _score) in enumerate(lexical):
            scores[record.id] = scores.get(record.id, 0.0) + 1.0 / (_RRF_K + rank + 1)

        confirmed: list[MemoryHitView] = []
        for memory_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
            record = await self._repo.get_record(user_id, memory_id)
            if record is None or record.kind not in selected:
                continue
            if record.status != "active" or record.valid_to is not None:
                continue
            if memory_id in revisions and revisions[memory_id] != record.revision:
                continue
            factor = _decay_factor(record)
            confirmed.append(
                MemoryHitView(record=record, score=score * factor)
            )

        page = _apply_quota(confirmed, selected, self._config, offset, limit)
        if page.items:
            await self._repo.touch_hits(
                user_id,
                [hit.record.id for hit in page.items],
                now=datetime.now(UTC),
            )
        return page

    async def _dense_candidates(
        self, user_id: str, text: str, kinds: Sequence[str], limit: int
    ) -> list[dict[str, Any]]:
        if not text.strip():
            return []
        try:
            vector = await self._embedder.embed_query(text)
            raw = await self._projection.search(
                user_id=user_id,
                kinds=list(kinds),
                vector=list(vector),
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 - lexical fallback is the contract
            logger.warning("memory vector retrieval failed (%s)", type(exc).__name__)
            return []
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            raw = raw[0]
        hits: list[dict[str, Any]] = []
        for row in raw or []:
            if hasattr(row, "entity"):
                row = getattr(row, "entity")
            if isinstance(row, dict) and row.get("memory_id"):
                hits.append(dict(row))
        return hits


def _apply_quota(
    hits: list[MemoryHitView],
    kinds: Sequence[str],
    config: MemoryConfig,
    offset: int,
    limit: int,
) -> MemorySearchPage:
    quotas = config.retrieval.per_kind_quota
    by_kind: dict[str, list[MemoryHitView]] = {kind: [] for kind in kinds}
    for hit in hits:
        by_kind.setdefault(hit.record.kind, []).append(hit)
    selected: list[MemoryHitView] = []
    for kind in kinds:
        selected.extend(by_kind.get(kind, [])[: int(quotas.get(kind, limit))])
    selected.sort(key=lambda hit: (-(hit.score or 0.0), hit.record.id))
    page = selected[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(selected) else None
    return MemorySearchPage(items=page, next_cursor=next_cursor)


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError as exc:
        raise ValueError("invalid memory cursor") from exc


def _decay_factor(record: MemoryRecordView) -> float:
    age_days = _age_days(record)
    half_life = _HALF_LIFE_DAYS.get(record.kind, 30.0)
    retention = 0.5 ** (age_days / half_life)
    return 0.5 + 0.5 * retention


def _age_days(record: MemoryRecordView) -> float:
    value = record.last_active_at or record.updated_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - value).total_seconds() / 86400.0)
