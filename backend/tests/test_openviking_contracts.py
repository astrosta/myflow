"""Contracts and config for the OpenViking memory adapter."""

from __future__ import annotations

from agentkit.agents.memory.openviking.config import (
    DEFAULT_MEMORY_TYPES,
    DEFAULT_SESSION_MEMORY_TYPES,
    OpenVikingMemoryConfig,
    idle_only_auto_commit_policy,
)
from agentkit.config.memory_config import MemoryConfig


def test_idle_only_policy_disables_thresholds() -> None:
    policy = idle_only_auto_commit_policy()

    assert policy["pending_token_threshold"] == 0
    assert policy["message_count_threshold"] == 0
    assert policy["idle_timeout_seconds"] == 86400
    assert policy["keep_recent_count"] == 0
    assert policy["min_commit_interval_seconds"] == 0

    assert "profile" not in DEFAULT_MEMORY_TYPES
    assert "identity" not in DEFAULT_MEMORY_TYPES
    assert "soul" not in DEFAULT_MEMORY_TYPES
    # The memory catalog has seven queryable types; session skills are separate.
    assert "tools" in DEFAULT_MEMORY_TYPES
    assert "skills" not in DEFAULT_MEMORY_TYPES
    assert DEFAULT_MEMORY_TYPES == (
        "preferences",
        "entities",
        "events",
        "cases",
        "trajectories",
        "experiences",
        "tools",
    )
    # Session creation always starts from the stock 0.5.2 policy.
    assert "tools" not in DEFAULT_SESSION_MEMORY_TYPES
    assert "skills" not in DEFAULT_SESSION_MEMORY_TYPES


def test_default_config_uses_explicit_session_policy() -> None:
    config = OpenVikingMemoryConfig()
    options = config.session_create_options()

    assert options["auto_commit_policy"]["idle_timeout_seconds"] == 86400
    assert options["auto_commit_policy"]["pending_token_threshold"] == 0
    assert options["memory_policy"]["working_memory"] == {"enabled": False}
    assert options["memory_policy"]["peer"] == {"enabled": False}
    assert options["memory_policy"]["self"] == {"enabled": True}
    assert options["memory_policy"]["memory_types"] == list(
        DEFAULT_SESSION_MEMORY_TYPES
    )


def test_session_policy_rejects_tools_and_skills() -> None:
    import pytest

    for memory_type in ("tools", "skills"):
        with pytest.raises(ValueError, match="unsupported memory types"):
            OpenVikingMemoryConfig(
                session={"memory_types": [*DEFAULT_SESSION_MEMORY_TYPES, memory_type]}
            )
    with pytest.raises(ValueError, match="DEFAULT_SESSION_MEMORY_TYPES"):
        OpenVikingMemoryConfig(
            session={"memory_types": [DEFAULT_SESSION_MEMORY_TYPES[0]]}
        )


def test_session_skill_import_defaults_are_bounded() -> None:
    config = OpenVikingMemoryConfig()

    assert config.session_skills.enabled is True
    assert config.session_skills.poll_interval_seconds == 2.0
    assert config.session_skills.task_timeout_seconds == 180.0
    assert config.session_skills.max_skills_per_task == 20


def test_openviking_switch_defaults_off_and_can_be_enabled() -> None:
    assert OpenVikingMemoryConfig().enabled is False
    assert MemoryConfig().openviking.enabled is False
    enabled = MemoryConfig(openviking={"enabled": True, "user_id_salt": "s" * 32})
    assert enabled.openviking.enabled is True
    assert enabled.openviking.session_create_options()["auto_commit_policy"]["idle_timeout_seconds"] == 86400


def test_extra_skip_tokens_are_validated_at_config_load() -> None:
    import pytest

    with pytest.raises(ValueError, match="task-context"):
        OpenVikingMemoryConfig(capture={"extra_skip_tokens": ("好的",)})


def test_retention_dedup_ttl_cannot_be_shorter_than_session_ttl() -> None:
    config = OpenVikingMemoryConfig(
        retention={"session_days": 30, "dedup_days": 7, "suggestion_days": 7}
    )

    assert config.retention.dedup_days == 30
