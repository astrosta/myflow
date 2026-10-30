"""Assistant identity/soul are project files, not OpenViking memory."""

from __future__ import annotations

from pathlib import Path

from agentkit.agents.leader_agent.agent import agent_cache_key
from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.config import AppConfig


def _app(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
    identity = tmp_path / "identity.md"
    soul = tmp_path / "soul.md"
    identity.write_text("名称：MyFlow\n气质：冷静直接\n", encoding="utf-8")
    soul.write_text("核心原则：先验证再回答。\n", encoding="utf-8")
    return AppConfig.model_validate(
        {
            "models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
            "memory": {
                "openviking": {
                    "enabled": enabled,
                    "user_id_salt": "s" * 32,
                    "personalization": {
                        "identity_file": str(identity),
                        "soul_file": str(soul),
                    },
                }
            },
        }
    )


def test_prompt_contains_identity_and_soul_when_openviking_is_enabled(tmp_path: Path) -> None:
    prompt = apply_prompt_template(app_config=_app(tmp_path))

    assert '<assistant-identity source="' in prompt
    assert '<assistant-soul source="' in prompt
    assert "冷静直接" in prompt
    assert "先验证再回答" in prompt


def test_prompt_keeps_the_slot_empty_when_openviking_is_disabled(tmp_path: Path) -> None:
    prompt = apply_prompt_template(app_config=_app(tmp_path, enabled=False))

    assert "<assistant-identity" not in prompt
    assert "<assistant-soul" not in prompt


def test_agent_cache_key_tracks_identity_file_change(tmp_path: Path) -> None:
    app = _app(tmp_path)
    first = agent_cache_key({}, app_config=app, checkpointer=None, user_id="u1")

    (tmp_path / "soul.md").write_text("核心原则：新的原则。\n", encoding="utf-8")
    second = agent_cache_key({}, app_config=app, checkpointer=None, user_id="u1")

    assert first.personalization_digest != second.personalization_digest
    assert first != second
