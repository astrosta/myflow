"""Per-kind maintenance policy (M10 section 2.9.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agentkit.agents.memory.types import MemoryKind, MemoryRecord, SemanticPayload, ensure_utc
from agentkit.config.memory_config import MemoryMaintenanceConfig



@dataclass(frozen=True, slots=True)
class ArchiveCandidate:
    record: MemoryRecord
    payload: SemanticPayload
    vitality: float
    age_days: float
    reason: str


def archive_candidate(
    record: MemoryRecord,
    *,
    now: datetime,
    config: MemoryMaintenanceConfig,
    vitality: float,
    payload: SemanticPayload,
) -> ArchiveCandidate | None:
    if record.kind != MemoryKind.SEMANTIC or record.status != "active":
        return None
    if record.explicit_remember or record.pinned or payload.explicit_remember or payload.profile_linked:
        return None
    age_days = max(
        0.0,
        (ensure_utc(now) - ensure_utc(record.created_at)).total_seconds() / 86_400.0,
    )
    minimum_age = (
        config.explicit_min_age_days if payload.source == "user_explicit" else config.min_age_days
    )
    if age_days < minimum_age:
        return None
    if vitality >= config.min_vitality:
        return None
    return ArchiveCandidate(
        record=record,
        payload=payload,
        vitality=vitality,
        age_days=age_days,
        reason="low_vitality",
    )


def retention_deadline(now: datetime, config: MemoryMaintenanceConfig) -> datetime:
    return ensure_utc(now) - timedelta(days=config.archive_retention_days)
