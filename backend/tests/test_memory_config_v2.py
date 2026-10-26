"""Task 1: canonical memory configuration and operator model resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.agents.memory.canonical.operator_model import (
    operator_model_name,
    resolve_operator_model,
)
from agentkit.config.memory_config import MemoryConfig


def test_spec_defaults_are_exact() -> None:
    config = MemoryConfig()
    assert config.canonical_store == "postgres"
    assert config.vector_store.collection == "myflow_memory_active"
    assert config.vector_store.consistency_level == "Strong"
    assert config.embedding.model == "Qwen/Qwen3-Embedding-8B"
    assert config.embedding.dimensions == 4096
    assert config.profile.auto_write_threshold == 0.85
    assert config.semantic.auto_activate_threshold == 0.80
    assert config.semantic.pending_review_ttl_days == 30
    assert config.episodic.session_idle_close_minutes == 30
    assert config.episodic.max_turns_per_session == 40
    assert config.episodic.gate_threshold == 0.55
    assert config.episodic.max_summary_tokens == 1200
    assert config.episodic.ttl_days == 180
    assert config.skill_candidates.min_source_episodes == 3
    assert config.skill_candidates.min_distinct_threads == 2
    assert config.skill_candidates.min_success_rate == 0.80
    assert config.skill_candidates.trigger_similarity_threshold == 0.82
    assert config.skill_candidates.strong_trigger_similarity == 0.92
    assert config.skill_candidates.min_common_steps == 2
    assert config.skill_candidates.expire_days == 30
    assert config.retrieval.per_kind_quota == {"semantic": 6, "episodic": 2}


def test_legacy_profile_alias_still_parses() -> None:
    config = MemoryConfig.model_validate({"profile": {"injection_enabled": False}})
    assert config.profile.inject is False
    assert config.profile.injection_enabled is False


def test_operator_model_uses_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_create_chat_model(**kwargs: object) -> object:
        calls.update(kwargs)
        return object()

    monkeypatch.setattr(
        "agentkit.agents.memory.canonical.operator_model.create_chat_model",
        fake_create_chat_model,
    )
    app = SimpleNamespace(
        agents=SimpleNamespace(default_model="agent-default"),
        models=[SimpleNamespace(name="fallback")],
    )
    assert resolve_operator_model(app) is not None
    assert calls == {
        "name": "agent-default",
        "thinking_enabled": False,
        "attach_tracing": False,
        "app_config": app,
    }


def test_operator_model_falls_back_to_first_model() -> None:
    app = SimpleNamespace(
        agents=SimpleNamespace(default_model=None),
        models=[SimpleNamespace(name="first"), SimpleNamespace(name="second")],
    )
    assert operator_model_name(app) == "first"


def test_operator_model_requires_a_model() -> None:
    app = SimpleNamespace(agents=SimpleNamespace(default_model=None), models=[])
    with pytest.raises(Exception, match="model"):
        operator_model_name(app)
