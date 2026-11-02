"""Shared bounded user-profile schema for the OpenViking adapter.

The field list and its validation live in :mod:`agentkit.agents.memory.profile_schema`;
this module stays as the stable import site for the memory packages and keeps the
historical names (`PROFILE_FIELD_PRIORITY`, `validate_profile_value`, ...).

Only the stable profile fields and their validation live here. Memory record
models, backends, operators and canonical types were removed with the legacy
canonical runtime.
"""

from __future__ import annotations

from agentkit.agents.memory.profile_schema import (
    EXPLICIT_ONLY_FIELDS,
    FOLLOW_CONVERSATION_LANGUAGE,
    PROFILE_FIELD_BY_PATH,
    PROFILE_FIELD_PATHS,
    PROFILE_FIELD_PRIORITY,
    PROFILE_FIELD_SPECS,
    SENSITIVE_VALUE_PATTERNS,
    ProfileFieldSpec,
    field_descriptions,
    looks_like_secret,
    validate_profile_value,
)

__all__ = [
    "EXPLICIT_ONLY_FIELDS",
    "FOLLOW_CONVERSATION_LANGUAGE",
    "PROFILE_FIELD_BY_PATH",
    "PROFILE_FIELD_PATHS",
    "PROFILE_FIELD_PRIORITY",
    "PROFILE_FIELD_SPECS",
    "ProfileFieldSpec",
    "SENSITIVE_VALUE_PATTERNS",
    "field_descriptions",
    "looks_like_secret",
    "validate_profile_value",
]
