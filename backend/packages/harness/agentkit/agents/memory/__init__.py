"""Shared long-term-memory profile schema.

The legacy canonical runtime and its manager/backend/services were removed.
OpenViking lives in the sibling openviking package.
"""

from agentkit.agents.memory.types import (
    EXPLICIT_ONLY_FIELDS,
    PROFILE_FIELD_PATHS,
    PROFILE_FIELD_PRIORITY,
    SENSITIVE_VALUE_PATTERNS,
    looks_like_secret,
    validate_profile_value,
)

__all__ = [
    "EXPLICIT_ONLY_FIELDS",
    "PROFILE_FIELD_PATHS",
    "PROFILE_FIELD_PRIORITY",
    "SENSITIVE_VALUE_PATTERNS",
    "looks_like_secret",
    "validate_profile_value",
]
