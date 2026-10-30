"""Shared bounded user-profile schema for the OpenViking adapter.

Only the stable profile fields and their validation live here. Memory record
models, backends, operators and canonical types were removed with the legacy
canonical runtime.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXPLICIT_ONLY_FIELDS = frozenset({
    "identity.preferred_name",
    "identity.pronouns",
    "accessibility.needs",
    "boundaries",
})

PROFILE_FIELD_PRIORITY: tuple[str, ...] = (
    "identity.preferred_name",
    "identity.pronouns",
    "identity.locale",
    "identity.timezone",
    "communication.language",
    "accessibility.needs",
    "boundaries",
    "communication.tone",
    "communication.verbosity",
    "communication.format",
    "work.role",
    "work.domains",
    "work.tools",
    "work.expertise",
)
PROFILE_FIELD_PATHS = frozenset(PROFILE_FIELD_PRIORITY)

SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"d{3}-d{2}-d{4}"),
)

_VALID_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_VALID_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)*$")


def _max_length(path: str) -> int:
    if path == "identity.preferred_name":
        return 64
    if path == "identity.pronouns":
        return 32
    if path == "work.role":
        return 100
    return 256


def _validate_list(value: Any, *, path: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    if len(value) > max_items:
        raise ValueError(f"{path} accepts at most {max_items} items")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path} items must be non-empty strings")
        if len(item) > max_chars:
            raise ValueError(f"{path} items must be at most {max_chars} characters")
        cleaned.append(item.strip())
    return cleaned


def validate_profile_value(path: str, value: Any) -> Any:
    """Validate one bounded profile field. Values are never free-form notes."""

    if path not in PROFILE_FIELD_PATHS:
        raise ValueError(f"unknown profile field: {path}")
    if path in EXPLICIT_ONLY_FIELDS and value is None:
        return None
    if path == "communication.format":
        allowed = {"bullets", "tables", "steps", "prose", "code_first"}
        if not isinstance(value, list) or not value or len(value) > len(allowed):
            raise ValueError(f"{path} must be a non-empty list of known formats")
        if any(item not in allowed for item in value):
            raise ValueError(f"{path} contains an unknown format")
        return list(dict.fromkeys(value))
    if path in {"work.domains", "work.tools", "accessibility.needs", "boundaries"}:
        limits = {
            "work.domains": (10, 64),
            "work.tools": (20, 64),
            "accessibility.needs": (10, 120),
            "boundaries": (10, 200),
        }
        return _validate_list(
            value,
            path=path,
            max_items=limits[path][0],
            max_chars=limits[path][1],
        )
    if path == "communication.tone":
        if value not in {"neutral", "friendly", "formal", "direct"}:
            raise ValueError("communication.tone must be neutral, friendly, formal, or direct")
        return value
    if path == "communication.verbosity":
        if value not in {"concise", "balanced", "detailed"}:
            raise ValueError("communication.verbosity must be concise, balanced, or detailed")
        return value
    if path == "work.expertise":
        if value not in {"beginner", "intermediate", "expert"}:
            raise ValueError("work.expertise must be beginner, intermediate, or expert")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{path} must not be empty")
    limit = _max_length(path)
    if len(text) > limit:
        raise ValueError(f"{path} must be at most {limit} characters")
    if path in {"identity.locale", "communication.language"}:
        if not _VALID_LOCALE.fullmatch(text):
            raise ValueError(f"{path} must be a BCP-47 language tag")
    if path == "identity.timezone":
        if not _VALID_TIMEZONE.fullmatch(text):
            raise ValueError("identity.timezone must be an IANA timezone")
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("identity.timezone must be a known IANA timezone") from exc
    if looks_like_secret(text):
        raise ValueError(f"{path} looks like a credential and must not be stored")
    if "[REDACTED]" in text or "[[PII:" in text:
        raise ValueError(f"{path} must not contain a redaction marker")
    return text


def looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS)


__all__ = [
    "EXPLICIT_ONLY_FIELDS",
    "PROFILE_FIELD_PATHS",
    "PROFILE_FIELD_PRIORITY",
    "SENSITIVE_VALUE_PATTERNS",
    "looks_like_secret",
    "validate_profile_value",
]
