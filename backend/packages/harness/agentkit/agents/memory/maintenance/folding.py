"""High-density semantic folding (M10 section 2.9.3).

Only active, unprotected semantic facts are candidates. A cluster is either
folded whole or left untouched: no partial merges, no silent fact invention.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from agentkit.agents.memory.types import (
    MemoryRecord,
    MemoryScope,
    SemanticPayload,
    ensure_utc,
    utc_now,
)
from agentkit.agents.memory.tokens import count_tokens
from agentkit.config.memory_config import MemoryFoldingConfig
from agentkit.privacy import PIIRedactor, has_redaction_marker

_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class FoldCluster:
    records: tuple[MemoryRecord, ...]
    topic_key: str | None


@dataclass(frozen=True, slots=True)
class FoldResult:
    cluster: FoldCluster
    summary: str
    source_fact_ids: tuple[str, ...]


def candidate_clusters(
    records: Iterable[MemoryRecord],
    *,
    config: MemoryFoldingConfig,
    now: datetime,
) -> list[FoldCluster]:
    active = [
        record
        for record in records
        if record.status == "active"
        and not record.explicit_remember
        and not record.pinned
        and not record.superseded_by
        and _payload(record).profile_linked is None
        and _payload(record).fold_depth < config.max_fold_depth
        and _age_days(record.created_at, now) > 7.0
    ]
    if len(active) <= config.min_facts:
        # Small stores only fold when one topic has enough fragments to matter.
        pass
    clusters: list[FoldCluster] = []
    by_topic: dict[str, list[MemoryRecord]] = {}
    for record in active:
        if record.topic_key:
            by_topic.setdefault(record.topic_key, []).append(record)
        elif len(active) > config.min_facts:
            clusters.extend(_lexical_clusters(record, active, config))
    for topic, members in by_topic.items():
        if len(members) >= config.min_topic_fragments:
            clusters.append(FoldCluster(tuple(members[: config.max_cluster_size]), topic))
    unique: dict[tuple[str, ...], FoldCluster] = {}
    for cluster in clusters:
        if not (config.min_cluster_size <= len(cluster.records) <= config.max_cluster_size):
            continue
        key = tuple(sorted(record.id for record in cluster.records))
        unique.setdefault(key, cluster)
    return list(unique.values())


def fold_cluster(
    cluster: FoldCluster,
    *,
    summarizer: Callable[[list[str]], str | None],
    config: MemoryFoldingConfig,
    now: datetime | None = None,
) -> FoldResult | None:
    now = now or utc_now()
    sources = [record.content for record in cluster.records]
    summary = summarizer(sources)
    if not summary or not summary.strip():
        return None
    summary = PIIRedactor().redact_for_memory(summary)
    if has_redaction_marker(summary):
        return None
    source_tokens = sum(count_tokens(text) for text in sources)
    summary_tokens = count_tokens(summary)
    if source_tokens and summary_tokens > math.floor(source_tokens * config.max_summary_token_ratio):
        return None
    if len(cluster.records) < config.min_cluster_size:
        return None
    source_ids = tuple(record.id for record in cluster.records)
    return FoldResult(cluster=cluster, summary=summary.strip(), source_fact_ids=source_ids)


def build_folded_record(
    scope: MemoryScope,
    result: FoldResult,
    *,
    now: datetime | None = None,
) -> MemoryRecord:
    now = now or utc_now()
    first = result.cluster.records[0]
    payload = SemanticPayload(
        content=result.summary,
        category=_payload(first).category,
        topic_key=result.cluster.topic_key,
        confidence=min(_payload(record).confidence for record in result.cluster.records),
        source="consolidated",
        status="active",
        source_fact_ids=list(result.source_fact_ids),
        fold_depth=max(_payload(record).fold_depth for record in result.cluster.records) + 1,
        valid_from=min(
            (record.valid_from or record.created_at for record in result.cluster.records),
            key=ensure_utc,
        ),
    )
    material = f"{scope.user_id}|{result.cluster.topic_key or ''}|{result.summary}"
    record_id = "fact_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return MemoryRecord(
        id=record_id,
        user_id=scope.user_id,
        kind="semantic",
        subtype="fact",
        content=result.summary,
        structured=payload.model_dump(mode="json"),
        search_text=result.summary,
        source="consolidated",
        confidence=payload.confidence,
        status="active",
        created_at=now,
        updated_at=now,
        topic_key=result.cluster.topic_key,
        agent_name=scope.agent_name,
        thread_id=scope.thread_id,
        metadata={"folded": True},
    )


def _payload(record: MemoryRecord) -> SemanticPayload:
    return SemanticPayload.model_validate(record.structured)


def _age_days(value: datetime, now: datetime) -> float:
    return max(0.0, (ensure_utc(now) - ensure_utc(value)).total_seconds() / 86_400.0)


def _lexical_clusters(record: MemoryRecord, active: list[MemoryRecord], config: MemoryFoldingConfig) -> list[FoldCluster]:
    tokens = set(_TOKEN_RE.findall(record.content.lower()))
    if not tokens:
        return []
    members = [record]
    for other in active:
        if other.id == record.id:
            continue
        if record.agent_name != other.agent_name:
            continue
        other_tokens = set(_TOKEN_RE.findall(other.content.lower()))
        if not other_tokens:
            continue
        similarity = len(tokens & other_tokens) / len(tokens | other_tokens)
        if similarity >= config.similarity_threshold:
            members.append(other)
    if len(members) < config.min_cluster_size:
        return []
    return [FoldCluster(tuple(members[: config.max_cluster_size]), None)]
