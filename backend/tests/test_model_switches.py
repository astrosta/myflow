"""网关对客户端装配开关的校验：模型白名单与推理等级都来自 config.yaml。"""

from __future__ import annotations

import pytest
from agentkit.config import AppConfig, ModelConfig
from backend.app.gateway.services import (
    ModelNotAllowed,
    RunRequestInvalid,
    _check_model_allowed,
    _check_reasoning_effort,
    _effective_model_name,
)


def _config(**overrides) -> AppConfig:
    first = {
        "name": "flash",
        "use": "langchain_openai:ChatOpenAI",
        "model": "deepseek-v4-flash",
        "api_key": "sk-test",
        "reasoning_efforts": ["low", {"id": "high", "label": "高"}],
    }
    first.update(overrides)
    second = {"name": "plain", "use": "langchain_openai:ChatOpenAI", "api_key": "sk-test"}
    return AppConfig(
        models=[ModelConfig.model_validate(first), ModelConfig.model_validate(second)],
        agents={"default_model": "plain"},
    )


def test_model_whitelist_is_the_configured_catalog() -> None:
    config = _config()
    assert _check_model_allowed("flash", config) == "flash"
    assert _check_model_allowed(None, config) is None
    with pytest.raises(ModelNotAllowed):
        _check_model_allowed("ghost", config)


def test_vendor_qualified_names_are_allowed_when_the_entry_exists() -> None:
    config = _config()
    # 厂商目录里新报出来的模型：复用 flash 那条配置，只换 vendor 侧模型名
    assert _check_model_allowed("flash@deepseek-reasoner", config) == "flash@deepseek-reasoner"
    with pytest.raises(ModelNotAllowed):
        _check_model_allowed("ghost@deepseek-reasoner", config)
    with pytest.raises(ModelNotAllowed):
        _check_model_allowed("flash@bad id", config)
    with pytest.raises(ModelNotAllowed):
        _check_model_allowed("flash@", config)


def test_vendor_qualified_names_keep_the_entry_config() -> None:
    config = _config()
    # 等级按 `<name>` 那条配置校验，和裸名字完全一致
    assert _check_reasoning_effort("high", "flash@deepseek-reasoner", config) == "high"
    with pytest.raises(RunRequestInvalid):
        _check_reasoning_effort("medium", "flash@deepseek-reasoner", config)


def test_effective_model_falls_back_like_the_agent_assembly() -> None:
    assert _effective_model_name("flash", _config()) == "flash"
    assert _effective_model_name(None, _config()) == "plain"
    assert _effective_model_name(None, AppConfig(models=[ModelConfig(name="only", use="a:B")])) == "only"


def test_reasoning_effort_must_be_published_by_the_selected_model() -> None:
    config = _config()
    assert _check_reasoning_effort(None, "flash", config) is None
    assert _check_reasoning_effort("low", "flash", config) == "low"

    with pytest.raises(RunRequestInvalid) as excinfo:
        _check_reasoning_effort("medium", "flash", config)
    assert "allowed" in str(excinfo.value)

    with pytest.raises(RunRequestInvalid):
        _check_reasoning_effort("low", "plain", config)

    # 没选模型时按 agents.default_model 校验：那里不支持就直接拒绝，
    # 而不是悄悄收下一个不会生效的等级。
    with pytest.raises(RunRequestInvalid):
        _check_reasoning_effort("high", None, config)

    default_supports = AppConfig(
        models=[ModelConfig.model_validate({"name": "flash", "use": "a:B", "reasoning_efforts": ["low", "high"]})],
        agents={"default_model": "flash"},
    )
    assert _check_reasoning_effort("high", None, default_supports) == "high"


def test_reasoning_effort_without_published_levels_accepts_any_supported_value() -> None:
    config = AppConfig(
        models=[
            ModelConfig.model_validate(
                {
                    "name": "hybrid",
                    "use": "langchain_openai:ChatOpenAI",
                    "api_key": "sk-test",
                    "supports_reasoning_effort": True,
                }
            )
        ]
    )
    assert _check_reasoning_effort("medium", "hybrid", config) == "medium"
