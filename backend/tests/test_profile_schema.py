"""The profile schema is the single source of truth for fields and validation."""

from __future__ import annotations

import pytest

from agentkit.agents.memory import types as legacy_types
from agentkit.agents.memory.profile_schema import (
    EXPLICIT_ONLY_FIELDS,
    FOLLOW_CONVERSATION_LANGUAGE,
    PROFILE_FIELD_BY_PATH,
    PROFILE_FIELD_PATHS,
    PROFILE_FIELD_PRIORITY,
    PROFILE_FIELD_SPECS,
    field_descriptions,
    validate_profile_value,
)


def test_schema_exports_all_supported_fields_in_priority_order() -> None:
    assert len(PROFILE_FIELD_SPECS) == 12
    assert PROFILE_FIELD_PRIORITY == tuple(spec.path for spec in PROFILE_FIELD_SPECS)
    # 2026-11-02：代词与时区已从画像 schema 移除
    assert "identity.pronouns" not in PROFILE_FIELD_PATHS
    assert "identity.timezone" not in PROFILE_FIELD_PATHS
    assert PROFILE_FIELD_PATHS == set(PROFILE_FIELD_PRIORITY)
    assert set(PROFILE_FIELD_BY_PATH) == PROFILE_FIELD_PATHS
    # legacy import site must keep working for existing call sites
    assert legacy_types.PROFILE_FIELD_PRIORITY == PROFILE_FIELD_PRIORITY


def test_every_spec_is_displayable_and_described() -> None:
    for spec in PROFILE_FIELD_SPECS:
        assert spec.label.strip()
        assert spec.group_label.strip()
        assert spec.description.strip()
        assert spec.kind in {
            "text",
            "locale",
            "language",
            "choice",
            "multi_choice",
            "string_list",
        }
        values = [option.value for option in spec.options]
        assert len(values) == len(set(values))
        assert spec.explicit_only is (spec.path in EXPLICIT_ONLY_FIELDS)


def test_field_descriptions_cover_every_field_and_option() -> None:
    lines = field_descriptions()
    assert len(lines) == len(PROFILE_FIELD_SPECS)
    joined = "\n".join(lines)
    for spec in PROFILE_FIELD_SPECS:
        assert spec.path in joined
        for option in spec.options:
            assert option.value in joined


def test_language_accepts_bcp47_and_follow_conversation() -> None:
    assert validate_profile_value("communication.language", "zh-CN") == "zh-CN"
    assert (
        validate_profile_value("communication.language", FOLLOW_CONVERSATION_LANGUAGE)
        == FOLLOW_CONVERSATION_LANGUAGE
    )
    with pytest.raises(ValueError, match="BCP-47"):
        validate_profile_value("communication.language", "中文")


def test_choice_fields_reject_unknown_values_and_list_allowed_ones() -> None:
    assert validate_profile_value("communication.tone", "direct") == "direct"
    with pytest.raises(ValueError, match="neutral"):
        validate_profile_value("communication.tone", "sarcastic")
    with pytest.raises(ValueError, match="beginner"):
        validate_profile_value("work.expertise", "guru")


def test_multi_choice_requires_known_values_and_dedupes() -> None:
    assert validate_profile_value("communication.format", ["steps", "bullets", "steps"]) == [
        "steps",
        "bullets",
    ]
    with pytest.raises(ValueError, match="bullets"):
        validate_profile_value("communication.format", ["pdf"])
    with pytest.raises(ValueError, match="non-empty"):
        validate_profile_value("communication.format", [])


def test_string_lists_are_bounded() -> None:
    assert validate_profile_value("work.tools", [" FastAPI ", "Postgres"]) == [
        "FastAPI",
        "Postgres",
    ]
    with pytest.raises(ValueError, match="must be a list"):
        validate_profile_value("work.tools", "FastAPI")
    with pytest.raises(ValueError, match="at most 20 items"):
        validate_profile_value("work.tools", [f"tool-{index}" for index in range(21)])
    with pytest.raises(ValueError, match="non-empty"):
        validate_profile_value("work.tools", [" "])
    with pytest.raises(ValueError, match="at most 64 characters"):
        validate_profile_value("work.tools", ["x" * 65])


def test_text_fields_trim_and_reject_blanks_and_secrets() -> None:
    assert validate_profile_value("identity.preferred_name", "  小林 ") == "小林"
    with pytest.raises(ValueError, match="must not be empty"):
        validate_profile_value("identity.preferred_name", "   ")
    with pytest.raises(ValueError, match="at most 64 characters"):
        validate_profile_value("identity.preferred_name", "x" * 65)
    with pytest.raises(ValueError, match="credential"):
        validate_profile_value("boundaries", ["sk-" + "a" * 24])
    with pytest.raises(ValueError, match="redaction"):
        validate_profile_value("work.role", "[REDACTED]")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown profile field"):
        validate_profile_value("identity.favourite_colour", "blue")
