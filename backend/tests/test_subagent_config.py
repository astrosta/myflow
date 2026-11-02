"""M8 §4：配置（D1）。YAML 形状、token 预算耦合、dataclass 与模型名解析。

这批测试钉的是 D1 的契约：字段改名、默认值漂移、``[]`` 与 ``null`` 的语义被合并、
或者把 pydantic 模型塞进执行器，都应该先在这里红。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agentkit.config import (
    AppConfig,
    CustomSubagentConfig,
    SubagentsConfig,
    SubagentTokenBudgetConfig,
    resolve_token_budget_max_tokens,
)
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX
from agentkit.subagents.config import SubagentConfig, resolve_subagent_model_name


def _models() -> list[dict]:
    return [
        {"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"},
        {"name": "gpt-4o", "use": "langchain_openai:ChatOpenAI", "api_key": "sk-test"},
    ]


def _app(**subagents) -> AppConfig:
    return AppConfig.model_validate({"models": _models(), "subagents": subagents})


def test_subagents_section_defaults() -> None:
    cfg = _app()
    assert cfg.subagents.timeout_seconds == 1800
    assert cfg.subagents.max_turns is None  # null = 用每个类型自己的值
    assert cfg.subagents.max_total_per_run == 6
    assert cfg.subagents.token_budget.enabled is True
    assert cfg.subagents.token_budget.max_tokens is None
    assert cfg.subagents.token_budget.warn_threshold == 0.7
    assert cfg.subagents.agents == {}
    assert cfg.subagents.custom_agents == {}


def test_omitted_section_equals_the_empty_one() -> None:
    assert AppConfig.model_validate({"models": _models()}).subagents == SubagentsConfig()


@pytest.mark.parametrize("value", [1, 6, 50])
def test_max_total_per_run_accepts_the_documented_range(value: int) -> None:
    assert _app(max_total_per_run=value).subagents.max_total_per_run == value


@pytest.mark.parametrize("value", [0, -1, 51, 1000])
def test_max_total_per_run_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        _app(max_total_per_run=value)


def test_subagents_section_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _app(max_agents=3)


def test_custom_agent_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _app(custom_agents={"researcher": {"description": "x", "prompt": "y"}})


def test_documented_custom_agent_shape_round_trips() -> None:
    cfg = _app(
        custom_agents={
            "researcher": {
                "description": "深度调研一个主题",
                "system_prompt": "你是调研员",
                "tools": None,
                "disallowed_tools": ["task", "ask_clarification"],
                "skills": None,
                "model": "inherit",
                "max_turns": 80,
                "timeout_seconds": 900,
            }
        }
    )
    agent = cfg.subagents.custom_agents["researcher"]
    assert agent.description == "深度调研一个主题"
    assert agent.system_prompt == "你是调研员"
    assert agent.tools is None
    assert agent.skills is None
    assert agent.disallowed_tools == ["task", "ask_clarification"]
    assert agent.model == "inherit"
    assert agent.max_turns == 80
    assert agent.timeout_seconds == 900


def test_custom_agent_defaults_match_the_dataclass() -> None:
    agent = CustomSubagentConfig.model_validate({"description": "x"})
    default = SubagentConfig(name="x", description="x")
    assert agent.disallowed_tools == default.disallowed_tools == ["task"]
    assert agent.max_turns == default.max_turns
    assert agent.timeout_seconds == default.timeout_seconds
    assert agent.model == default.model == "inherit"
    assert agent.tools is None
    assert agent.skills is None


def test_custom_agent_keeps_an_empty_skill_list() -> None:
    """[] = 禁用技能，不能和 null（全部技能）合并。"""
    agent = CustomSubagentConfig.model_validate({"description": "x", "skills": []})
    assert agent.skills == []


def test_custom_agent_tools_null_means_inherit() -> None:
    assert CustomSubagentConfig.model_validate({"description": "x", "tools": None}).tools is None


def test_custom_agent_requires_a_description() -> None:
    with pytest.raises(ValidationError):
        CustomSubagentConfig.model_validate({})


def test_a_long_custom_description_is_accepted_with_a_warning(caplog) -> None:
    long_text = "x" * 601
    with caplog.at_level(logging.WARNING):
        agent = CustomSubagentConfig.model_validate({"description": long_text})
    assert agent.description == long_text
    assert "600" in caplog.text


def test_custom_agent_skills_field_is_documented_as_inert() -> None:
    assert "暂未生效" in CustomSubagentConfig.model_fields["skills"].description


def test_custom_agent_model_must_be_declared_or_inherit() -> None:
    _app(custom_agents={"researcher": {"description": "x", "model": "gpt-4o"}})
    with pytest.raises(ValidationError, match="researcher"):
        _app(custom_agents={"researcher": {"description": "x", "model": "kimi-k2.5"}})


def test_override_model_must_be_declared_or_inherit() -> None:
    _app(agents={"general-purpose": {"model": "inherit"}})
    _app(agents={"general-purpose": {"model": "gpt-4o"}})
    with pytest.raises(ValidationError, match="general-purpose"):
        _app(agents={"general-purpose": {"model": "kimi-k2.5"}})


def test_documented_override_shape_round_trips() -> None:
    cfg = _app(agents={"general-purpose": {"timeout_seconds": 1800, "max_turns": 150}})
    override = cfg.subagents.agents["general-purpose"]
    assert override.timeout_seconds == 1800
    assert override.max_turns == 150
    assert override.model is None
    assert override.token_budget is None


def test_override_cannot_redefine_a_type() -> None:
    """覆盖只动超时/轮次/模型/token 预算；description 与工具集属于 custom_agents。"""
    with pytest.raises(ValidationError):
        _app(agents={"general-purpose": {"system_prompt": "hi"}})


def test_override_accepts_a_token_budget_of_its_own() -> None:
    cfg = _app(agents={"general-purpose": {"token_budget": {"max_tokens": 250_000}}})
    budget = cfg.subagents.agents["general-purpose"].token_budget
    assert budget is not None and budget.max_tokens == 250_000


def test_token_budget_default_follows_summarization() -> None:
    off = _app()
    assert off.resolve_subagent_token_budget_max_tokens() == 2_000_000
    on = AppConfig.model_validate({"models": _models(), "summarization": {"enabled": True}})
    assert on.resolve_subagent_token_budget_max_tokens() == 1_000_000


def test_explicit_token_budget_max_tokens_always_wins() -> None:
    explicit = SubagentTokenBudgetConfig(max_tokens=500_000)
    assert resolve_token_budget_max_tokens(explicit, summarization_enabled=True) == 500_000
    assert resolve_token_budget_max_tokens(explicit, summarization_enabled=False) == 500_000
    cfg = AppConfig.model_validate(
        {
            "models": _models(),
            "summarization": {"enabled": True},
            "subagents": {"token_budget": {"max_tokens": 12_345}},
        }
    )
    assert cfg.resolve_subagent_token_budget_max_tokens() == 12_345


@pytest.mark.parametrize("value", [0, 1.5, -0.2])
def test_warn_threshold_stays_a_ratio(value: float) -> None:
    with pytest.raises(ValidationError):
        SubagentTokenBudgetConfig(warn_threshold=value)


def test_token_budget_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _app(token_budget={"max_token": 5})


def test_null_nested_entries_read_as_missing() -> None:
    """注释掉整段 YAML 会解析成 None（M0 §5.7）：当作缺失，走默认值。"""
    cfg = _app(token_budget=None, agents=None, custom_agents=None, max_turns=None)
    assert cfg.subagents == SubagentsConfig()


def test_null_agent_entry_is_ignored() -> None:
    cfg = _app(custom_agents={"researcher": None}, agents={"bash": None})
    assert cfg.subagents.custom_agents == {}
    assert cfg.subagents.agents == {}


def test_null_subagents_section_falls_back_to_defaults() -> None:
    cfg = AppConfig.model_validate({"models": _models(), "subagents": None})
    assert cfg.subagents == SubagentsConfig()


def test_subagents_section_is_hot_reloadable() -> None:
    assert "subagents" not in STARTUP_ONLY_FIELDS
    assert "subagents.max_total_per_run" not in STARTUP_ONLY_FIELDS
    description = SubagentsConfig.model_fields["max_total_per_run"].description or ""
    assert not description.startswith(STARTUP_ONLY_PREFIX)


def test_dataclass_defaults_and_list_independence() -> None:
    first = SubagentConfig(name="a", description="x")
    second = SubagentConfig(name="b", description="y")
    assert first.disallowed_tools == ["task"]
    assert first.max_turns == 50
    assert first.timeout_seconds == 900
    assert first.model == "inherit"
    assert first.system_prompt is None
    assert first.tools is None
    assert first.skills is None
    first.disallowed_tools.append("write_file")  # type: ignore[union-attr]
    assert second.disallowed_tools == ["task"], "默认值必须是每个实例一份"


def test_resolve_subagent_model_name_explicit_wins() -> None:
    config = SubagentConfig(name="researcher", description="x", model="gpt-4o")
    assert resolve_subagent_model_name(config, "deepseek-reasoner", app_config=_app()) == "gpt-4o"


def test_resolve_subagent_model_name_inherits_the_parent_name() -> None:
    config = SubagentConfig(name="researcher", description="x")
    assert resolve_subagent_model_name(config, "gpt-4o", app_config=_app()) == "gpt-4o"


def test_resolve_subagent_model_name_falls_back_to_the_first_model() -> None:
    config = SubagentConfig(name="researcher", description="x")
    assert resolve_subagent_model_name(config, None, app_config=_app()) == "deepseek-reasoner"


def test_resolve_subagent_model_name_without_models_fails_loudly() -> None:
    empty = AppConfig.model_validate({"models": []})
    with pytest.raises(ValueError, match="No chat models are configured."):
        resolve_subagent_model_name(SubagentConfig(name="r", description="x"), None, app_config=empty)


def test_example_config_declares_the_subagent_sections() -> None:
    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["subagents"]["max_total_per_run"] == 6
    assert data["subagents"]["token_budget"]["warn_threshold"] == 0.7
    assert data["subagent_runtime"]["max_running"] == 3
    assert data["subagent_runtime"]["admission_policy"] == "queue"
    AppConfig.model_validate(data)
