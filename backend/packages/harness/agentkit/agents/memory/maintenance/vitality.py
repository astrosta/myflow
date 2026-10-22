"""Ebbinghaus-inspired vitality scoring (M10 section 2.9.2)."""

from __future__ import annotations

import math
from datetime import datetime

from agentkit.agents.memory.types import (
    MemoryRecord,
    SemanticPayload,
    ensure_utc,
)

SOURCE_STABILITY_DAYS = {
    "user_explicit": 60.0,
    "user_confirmed": 45.0,
    "inferred": 30.0,
    "consolidated": 60.0,
    "import": 60.0,
}
SOURCE_WEIGHT = {
    "user_explicit": 1.0,
    "user_confirmed": 0.9,
    "inferred": 0.6,
    "consolidated": 0.9,
    "import": 0.9,
}
CATEGORY_WEIGHT = {
    "preference": 1.0,
    "project": 0.7,
    "context": 0.5,
    "profile": 0.8,
    "instruction": 1.0,
}


def payload_of(record: MemoryRecord) -> SemanticPayload:
    return SemanticPayload.model_validate(record.structured)


def last_touch(record: MemoryRecord, payload: SemanticPayload | None = None) -> datetime:
    values = [record.created_at]
    if payload is not None:
        if payload.last_accessed_at is not None:
            values.append(payload.last_accessed_at)
        if payload.last_confirmed_at is not None:
            values.append(payload.last_confirmed_at)
    return max(ensure_utc(value) for value in values)


def vitality_score(record: MemoryRecord, now: datetime, *, payload: SemanticPayload | None = None) -> float:
    payload = payload or payload_of(record)
    touched = last_touch(record, payload)
    elapsed_days = max(0.0, (ensure_utc(now) - touched).total_seconds() / 86_400.0)
    stability = (
        SOURCE_STABILITY_DAYS.get(payload.source, 30.0)
        * (1.0 + 0.6 * math.log1p(max(0, payload.access_count)))
        * (1.0 + 0.3 * max(0, payload.confirmation_count))
    )
    retention = math.exp(-elapsed_days / max(stability, 1e-9))
    importance = (
        max(0.0, min(1.0, payload.confidence))
        * SOURCE_WEIGHT.get(payload.source, 0.6)
        * CATEGORY_WEIGHT.get(payload.category, 0.5)
    )
    return round(retention * importance, 8)


def is_protected(record: MemoryRecord, payload: SemanticPayload | None = None) -> bool:
    payload = payload or payload_of(record)
    return bool(
        record.explicit_remember
        or record.pinned
        or payload.explicit_remember
        or payload.profile_linked
        or record.status != "active"
    )
