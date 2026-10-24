from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agentkit.config import AgentsConfig, AppConfig, CheckpointerConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX


def _models() -> list[dict]:
    return [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


def test_omitted_sections_use_m1_defaults():
    cfg = AppConfig.model_validate({"models": _models()})
    assert cfg.checkpointer.type == "memory"
    assert cfg.agents.default_model is None


def test_accepts_memory_checkpointer_and_agents_default_model():
    cfg = AppConfig.model_validate(
        {
            "models": _models(),
            "checkpointer": {"type": "memory"},
            "agents": {"default_model": "deepseek-reasoner"},
        }
    )
    assert cfg.checkpointer.type == "memory"
    assert cfg.agents.default_model == "deepseek-reasoner"


def test_checkpointer_section_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "checkpointer": {"type": "memory", "path": ".myflow"},
            }
        )


def test_agents_section_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "agents": {"default_model": "deepseek-reasoner", "name": "lead"},
            }
        )


def test_default_model_must_be_a_configured_model_name():
    with pytest.raises(ValidationError, match="default_model"):
        AppConfig.model_validate(
            {
                "models": _models(),
                "agents": {"default_model": "kimi-k2.5"},
            }
        )


def test_null_sections_fall_back_to_defaults():
    cfg = AppConfig.model_validate(
        {
            "models": _models(),
            "checkpointer": None,
            "agents": None,
        }
    )
    assert cfg.checkpointer.type == "memory"
    assert cfg.agents.default_model is None


def test_checkpointer_type_is_startup_only():
    assert "checkpointer.type" in STARTUP_ONLY_FIELDS
    description = CheckpointerConfig.model_fields["type"].description or ""
    reason = STARTUP_ONLY_FIELDS["checkpointer.type"]
    assert description.startswith(f"{STARTUP_ONLY_PREFIX} {reason}")


def test_agents_default_model_is_hot_reloadable():
    assert "agents" not in STARTUP_ONLY_FIELDS
    assert "agents.default_model" not in STARTUP_ONLY_FIELDS
    description = AgentsConfig.model_fields["default_model"].description or ""
    assert not description.startswith(STARTUP_ONLY_PREFIX)


def test_example_config_declares_m1_sections():
    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["checkpointer"]["type"] == "memory"
    names = {item["name"] for item in data["models"]}
    assert data["agents"]["default_model"] in names
    AppConfig.model_validate(data)
