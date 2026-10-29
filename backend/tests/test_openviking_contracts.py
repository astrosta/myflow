"""Contracts and config for the OpenViking memory adapter."""

from __future__ import annotations

from agentkit.agents.memory.openviking.config import (
    DEFAULT_MEMORY_TYPES,
    OpenVikingMemoryConfig,
    idle_only_auto_commit_policy,
)


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
    assert len(DEFAULT_MEMORY_TYPES) == 8


def test_default_config_uses_explicit_session_policy() -> None:
    config = OpenVikingMemoryConfig()
    options = config.session_create_options()

    assert options["auto_commit_policy"]["idle_timeout_seconds"] == 86400
    assert options["auto_commit_policy"]["pending_token_threshold"] == 0
    assert options["memory_policy"]["working_memory"] == {"enabled": False}
    assert options["memory_policy"]["peer"] == {"enabled": False}
    assert options["memory_policy"]["self"] == {"enabled": True}
    assert options["memory_policy"]["memory_types"] == list(DEFAULT_MEMORY_TYPES)


def test_retention_dedup_ttl_cannot_be_shorter_than_session_ttl() -> None:
    config = OpenVikingMemoryConfig(
        retention={"session_days": 30, "dedup_days": 7, "suggestion_days": 7}
    )

    assert config.retention.dedup_days == 30
