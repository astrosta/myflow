"""EvidenceTier is the only source of extraction confidence."""

from __future__ import annotations

from typing import Literal, cast

EvidenceTier = Literal[
    "EXPLICIT_MANDATE",
    "EXPLICIT_STATEMENT",
    "BEHAVIORAL_OBSERVED",
    "INFERRED_HYPOTHESIS",
]

_CONFIDENCE: dict[str, float] = {
    "EXPLICIT_MANDATE": 0.95,
    "EXPLICIT_STATEMENT": 0.85,
    "BEHAVIORAL_OBSERVED": 0.70,
    "INFERRED_HYPOTHESIS": 0.50,
}


def confidence_for_tier(tier: str) -> float:
    try:
        return _CONFIDENCE[tier]
    except KeyError as exc:
        raise ValueError(f"unknown evidence tier: {tier}") from exc


def tier_for_confidence(value: float) -> EvidenceTier | None:
    for tier, confidence in _CONFIDENCE.items():
        if abs(confidence - value) < 1e-9:
            return cast(EvidenceTier, tier)
    return None


def is_active_confidence(value: float) -> bool:
    return value >= 0.80


def is_pending_confidence(value: float) -> bool:
    return 0.50 <= value < 0.80


def is_profile_confidence(value: float) -> bool:
    return value >= 0.85
