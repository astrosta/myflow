"""Per-user ownership metadata stored in ContextForge tool annotations.

ContextForge's admin token is process-wide, so the upstream owner_email is the
admin account, not the MyFlow user. The stable MyFlow storage user id lives in
a reserved annotation instead. It is stripped from every browser-facing wire
response and from the tool editor; the gateway re-adds it on writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OWNER_ANNOTATION = "myflow_owner"


def owner_from_annotations(annotations: Mapping[str, Any] | None) -> str | None:
    if not isinstance(annotations, Mapping):
        return None
    value = annotations.get(OWNER_ANNOTATION)
    return value if isinstance(value, str) and value else None


def strip_owner(annotations: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(annotations, Mapping):
        return {}
    return {str(key): value for key, value in annotations.items() if key != OWNER_ANNOTATION}


def with_owner(annotations: Mapping[str, Any] | None, owner: str | None) -> dict[str, Any]:
    clean = strip_owner(annotations)
    if owner:
        clean[OWNER_ANNOTATION] = owner
    return clean


def can_view(owner: str | None, user_id: str | None) -> bool:
    """Unowned tools are claimable; owned tools are visible only to their owner."""
    if user_id is None or owner is None:
        return True
    return owner == user_id


def can_manage(owner: str | None, user_id: str | None) -> bool:
    """Only the owner may edit/delete/enable a tool; unowned tools are claim-only."""
    if user_id is None:
        return True
    return owner == user_id


def can_claim(owner: str | None, user_id: str | None) -> bool:
    if user_id is None:
        return True
    return owner is None or owner == user_id


def can_call(owner: str | None, user_id: str | None) -> bool:
    """Runtime tools are stricter than the management page: no unowned tools."""
    if user_id is None:
        return True
    return owner == user_id


__all__ = [
    "OWNER_ANNOTATION",
    "can_call",
    "can_claim",
    "can_manage",
    "can_view",
    "owner_from_annotations",
    "strip_owner",
    "with_owner",
]