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
    assert len(PROFILE_FIELD_SPECS) == 10
    assert PROFILE_FIELD_PRIORITY == tuple(spec.path for spec in PROFILE_FIELD_SPECS)
    # 2026-11-02：代词与时区已从画像 schema 移除
    assert "identity.pronouns" not in PROFILE_FIELD_PATHS
    assert "identity.timezone" not in PROFILE_FIELD_PATHS
    # 无障碍需求与边界已移除
    assert "accessibility.needs" not in PROFILE_FIELD_PATHS
    assert "boundaries" not in PROFILE_FIELD_PATHS
    assert PROFILE_FIELD_PATHS == set(PROFILE_FIELD_PRIORITY)
    assert set(PROFILE_FIELD_BY_PATH) == PROFILE_FIELD_PATHS
    # legacy import site must keep working for existing call sites
    assert legacy_types.PROFILE_FIELD_PRIORITY == PROFILE_FIELD_PRIORITY


def test_every_spec_is_displayable_and_free_form() -> None:
    for spec in PROFILE_FIELD_SPECS:
        assert spec.label.strip()
        assert spec.group_label.strip()
        assert spec.description.strip()
        # 只有自由文本和自由文本列表两种控件；不再有 choice/multi_choice。
        assert spec.kind in {"text", "string_list"}
        assert spec.group in {"identity", "communication", "work"}
        assert spec.explicit_only is (spec.path in EXPLICIT_ONLY_FIELDS)


def test_field_descriptions_cover_every_field_and_keep_hints_as_examples() -> None:
    lines = field_descriptions()
    assert len(lines) == len(PROFILE_FIELD_SPECS)
    joined = "\n".join(lines)
    for spec in PROFILE_FIELD_SPECS:
        assert spec.path in joined
    # 常见写法只是提示，不是允许值清单。
    assert "中性" in joined
    assert "要点" in joined
    assert "可自由填写" in joined
    assert "可选值" not in joined


def test_language_accepts_free_text_and_follow_conversation() -> None:
    assert validate_profile_value("communication.language", "zh-CN") == "zh-CN"
    assert (
        validate_profile_value("communication.language", FOLLOW_CONVERSATION_LANGUAGE)
        == FOLLOW_CONVERSATION_LANGUAGE
    )
    # 不再是枚举/BCP-47 校验：用户写中文标签也接受。
    assert validate_profile_value("communication.language", "简体中文") == "简体中文"


def test_formerly_enumerated_fields_accept_any_text() -> None:
    assert validate_profile_value("communication.tone", "温和") == "温和"
    assert validate_profile_value("communication.verbosity", "两三句话") == "两三句话"
    assert validate_profile_value("work.expertise", "资深") == "资深"


def test_list_fields_are_free_form_and_bounded() -> None:
    assert validate_profile_value("communication.format", ["要点", "表格", "要点"]) == [
        "要点",
        "表格",
        "要点",
    ]
    assert validate_profile_value("work.tools", [" FastAPI ", "Postgres"]) == [
        "FastAPI",
        "Postgres",
    ]
    with pytest.raises(ValueError, match="non-empty"):
        validate_profile_value("communication.format", [])
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
        validate_profile_value("work.role", "sk-" + "a" * 24)
    with pytest.raises(ValueError, match="credential"):
        validate_profile_value("work.tools", ["sk-" + "a" * 24])
    with pytest.raises(ValueError, match="redaction"):
        validate_profile_value("work.role", "[REDACTED]")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown profile field"):
        validate_profile_value("identity.favourite_colour", "blue")
