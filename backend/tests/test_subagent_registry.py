"""M8 §5（D2）：注册表解析顺序、两个内置、工具过滤、技能身份。

这里是"模型看到什么、能调什么"的唯一裁决点：发现列表与 ``task`` 执行必须给同一个答案，
否则要么看得见调不动（白烧轮次），要么看不见却能调（策略形同虚设）。
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from agentkit.config import AppConfig
from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.subagents.builtins import BUILTIN_SUBAGENTS
from agentkit.subagents.config import SubagentConfig
from agentkit.subagents.registry import (
    BASH_SUBAGENT_NAME,
    filter_tools,
    get_subagent_config,
    list_subagent_names,
    subagent_skill_registry,
)

LOCAL_PROVIDER = "agentkit.sandbox.local:LocalSandboxProvider"
ISOLATING_PROVIDER = "myflow.sandbox.docker:DockerSandboxProvider"


def _config(
    *,
    allow_host_bash: bool = False,
    use: str = LOCAL_PROVIDER,
    models: list[dict] | None = None,
    **subagents,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": models
            or [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {"use": use, "allow_host_bash": allow_host_bash},
            # 检索子代理（knowledge/memory/skills）另有用例覆盖；这里关掉技能检索，
            # 让断言只描述 general-purpose 与 bash 的发现顺序。
            "skills": {"search": {"enabled": False}},
            "subagents": subagents,
        }
    )


def _two_models() -> list[dict]:
    return [
        {"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"},
        {"name": "gpt-4o", "use": "langchain_openai:ChatOpenAI", "api_key": "sk-test"},
    ]


def _host_bash_config() -> AppConfig:
    """内置 bash 可用的配置：本地 provider + 显式打开宿主 bash。"""
    return _config(allow_host_bash=True)


def _tool(name: str) -> BaseTool:
    def run() -> str:
        return name

    return StructuredTool.from_function(run, name=name, description=name)


def _names(tools: list[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def test_builtin_order_is_the_discovery_order() -> None:
    assert list(BUILTIN_SUBAGENTS) == [
        "general-purpose",
        "knowledge-researcher",
        "memory-researcher",
        "skill-finder",
        "bash",
    ]
    assert list_subagent_names(app_config=_host_bash_config(), allowed=None) == [
        "general-purpose",
        "bash",
    ]


def test_general_purpose_defaults() -> None:
    resolved = get_subagent_config("general-purpose", app_config=_host_bash_config())
    assert resolved is not None
    assert resolved.tools is None  # 继承 lead 当时持有的工具
    assert resolved.disallowed_tools == ["task", "ask_clarification"]  # 禁止嵌套委派与提问
    assert resolved.model == "inherit"
    assert resolved.max_turns == 150
    assert resolved.timeout_seconds == 1800  # 段落值，不是 dataclass 的 900
    assert resolved.skills is None
    assert resolved.system_prompt


def test_bash_defaults() -> None:
    resolved = get_subagent_config("bash", app_config=_host_bash_config())
    assert resolved is not None
    assert resolved.tools == ["bash", "ls", "read_file", "write_file", "str_replace"]
    assert resolved.disallowed_tools == ["task", "ask_clarification"]
    assert resolved.max_turns == 60
    assert resolved.timeout_seconds == 1800


def test_unknown_type_resolves_to_none() -> None:
    assert get_subagent_config("nope", app_config=_host_bash_config()) is None


def test_custom_agent_uses_its_own_values() -> None:
    app_config = _config(
        custom_agents={
            "researcher": {
                "description": "深度调研",
                "system_prompt": "你是调研员",
                "tools": None,
                "skills": [],
                "model": "inherit",
                "max_turns": 80,
            }
        }
    )
    resolved = get_subagent_config("researcher", app_config=app_config)
    assert resolved == SubagentConfig(
        name="researcher",
        description="深度调研",
        system_prompt="你是调研员",
        tools=None,
        disallowed_tools=["task"],  # YAML 默认
        skills=[],
        model="inherit",
        max_turns=80,
        timeout_seconds=900,  # custom 用自己的默认值，不吃段落值
    )


def test_custom_agent_overrides_the_builtin_of_the_same_name(caplog: pytest.LogCaptureFixture) -> None:
    app_config = _config(custom_agents={"general-purpose": {"description": "mine"}})
    with caplog.at_level(logging.WARNING, logger="agentkit.subagents.registry"):
        resolved = get_subagent_config("general-purpose", app_config=app_config)
    assert resolved is not None
    assert resolved.description == "mine"
    assert resolved.max_turns == 50  # custom 的默认值，不再是内置的 150
    assert any("overrides the builtin" in record.getMessage() for record in caplog.records)


def test_section_values_only_fill_builtins() -> None:
    """§4.1：``subagents.timeout_seconds`` / ``max_turns`` 是**内置**的默认值。"""
    app_config = _config(
        timeout_seconds=600,
        max_turns=42,
        custom_agents={"researcher": {"description": "x", "max_turns": 80, "timeout_seconds": 120}},
    )
    builtin = get_subagent_config("general-purpose", app_config=app_config)
    assert (builtin.max_turns, builtin.timeout_seconds) == (42, 600)
    custom = get_subagent_config("researcher", app_config=app_config)
    assert (custom.max_turns, custom.timeout_seconds) == (80, 120)


def test_null_section_max_turns_keeps_the_builtin_value() -> None:
    app_config = _host_bash_config()
    assert get_subagent_config("general-purpose", app_config=app_config).max_turns == 150
    assert get_subagent_config("bash", app_config=app_config).max_turns == 60


def test_per_name_override_applies_last() -> None:
    app_config = _config(
        allow_host_bash=True,
        models=_two_models(),
        timeout_seconds=600,
        max_turns=42,
        agents={"general-purpose": {"timeout_seconds": 30, "max_turns": 5, "model": "gpt-4o"}},
    )
    resolved = get_subagent_config("general-purpose", app_config=app_config)
    assert (resolved.max_turns, resolved.timeout_seconds, resolved.model) == (5, 30, "gpt-4o")
    untouched = get_subagent_config("bash", app_config=app_config)
    assert (untouched.max_turns, untouched.timeout_seconds) == (42, 600)


def test_resolution_never_mutates_the_builtin_template() -> None:
    app_config = _config(agents={"general-purpose": {"max_turns": 5}})
    resolved = get_subagent_config("general-purpose", app_config=app_config)
    template = BUILTIN_SUBAGENTS["general-purpose"]
    assert resolved.disallowed_tools is not template.disallowed_tools
    resolved.disallowed_tools.append("write_file")
    assert template.disallowed_tools == ["task", "ask_clarification"]
    assert template.max_turns == 150


def test_bash_disappears_when_host_bash_is_disallowed() -> None:
    """§5.2：不要让模型看见却调不动。"""
    app_config = _config(allow_host_bash=False)
    assert list_subagent_names(app_config=app_config, allowed=None) == ["general-purpose"]
    assert get_subagent_config(BASH_SUBAGENT_NAME, app_config=app_config) is None


def test_bash_is_available_on_an_isolating_provider() -> None:
    app_config = _config(use=ISOLATING_PROVIDER, allow_host_bash=False)
    assert list_subagent_names(app_config=app_config, allowed=None) == ["general-purpose", "bash"]
    assert get_subagent_config(BASH_SUBAGENT_NAME, app_config=app_config) is not None


def test_a_custom_bash_survives_the_sandbox_gate() -> None:
    """自定义类型是用户自己写的定义，不因为内置的那个不可用而消失。"""
    app_config = _config(allow_host_bash=False, custom_agents={"bash": {"description": "我的 bash"}})
    assert list_subagent_names(app_config=app_config, allowed=None) == ["general-purpose", "bash"]
    resolved = get_subagent_config("bash", app_config=app_config)
    assert resolved is not None and resolved.description == "我的 bash"


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (None, ["general-purpose", "bash"]),
        ([], []),
        (["bash"], ["bash"]),
        (["bash", "general-purpose"], ["general-purpose", "bash"]),  # 保持发现顺序
        (["nope"], []),
    ],
)
def test_allowed_subagents_is_the_whitelist(allowed: list[str] | None, expected: list[str]) -> None:
    assert list_subagent_names(app_config=_host_bash_config(), allowed=allowed) == expected


def test_discovery_dedupes_an_overridden_builtin() -> None:
    app_config = _config(
        allow_host_bash=True, custom_agents={"general-purpose": {"description": "x"}}
    )
    assert list_subagent_names(app_config=app_config, allowed=None) == ["general-purpose", "bash"]


def test_custom_names_are_appended_after_builtins() -> None:
    app_config = _config(
        allow_host_bash=True,
        custom_agents={"researcher": {"description": "x"}, "writer": {"description": "y"}},
    )
    assert list_subagent_names(app_config=app_config, allowed=None) == [
        "general-purpose",
        "bash",
        "researcher",
        "writer",
    ]


def test_filter_tools_inherits_everything_by_default() -> None:
    tools = [_tool("bash"), _tool("read_file"), _tool("write_file")]
    assert filter_tools(tools, None, None) == tools


def test_filter_tools_applies_the_allow_list() -> None:
    tools = [_tool("bash"), _tool("read_file"), _tool("write_file"), _tool("task")]
    assert _names(filter_tools(tools, ["read_file", "write_file"], None)) == ["read_file", "write_file"]


def test_filter_tools_applies_the_deny_list_last() -> None:
    tools = [_tool("bash"), _tool("read_file"), _tool("write_file"), _tool("task")]
    assert _names(filter_tools(tools, None, ["task", "ask_clarification"])) == [
        "bash",
        "read_file",
        "write_file",
    ]
    assert _names(filter_tools(tools, ["bash", "task"], ["task"])) == ["bash"]


def test_filter_tools_skips_declared_tools_that_do_not_exist() -> None:
    """§5.2：``bash`` 的五个工具"有则留，无则跳过"（宿主 bash 关掉时 lead 就没有 bash）。"""
    resolved = get_subagent_config("bash", app_config=_host_bash_config())
    tools = [_tool("read_file"), _tool("ls"), _tool("task")]
    assert _names(filter_tools(tools, resolved.tools, resolved.disallowed_tools)) == ["read_file", "ls"]


def test_subagent_skill_registry_follows_the_parent_identity() -> None:
    app_config = _config(custom_agents={"researcher": {"description": "x", "skills": ["alpha"]}})
    resolved = get_subagent_config("researcher", app_config=app_config)
    registry = subagent_skill_registry(resolved, app_config=app_config, user_id="alice")
    assert registry.user_id == "alice"
    assert registry.available_skills == frozenset({"alpha"})


def test_subagent_skill_registry_falls_back_to_the_default_bucket() -> None:
    app_config = _config(custom_agents={"researcher": {"description": "x"}})
    resolved = get_subagent_config("researcher", app_config=app_config)
    assert subagent_skill_registry(resolved, app_config=app_config).user_id == DEFAULT_USER_ID
    assert subagent_skill_registry(resolved, app_config=app_config).available_skills is None
    assert subagent_skill_registry(
        resolved, app_config=app_config, runtime_context={"user_id": "bob"}
    ).user_id == "bob"


def test_empty_skill_list_means_no_skills_not_all_skills() -> None:
    app_config = _config(custom_agents={"researcher": {"description": "x", "skills": []}})
    resolved = get_subagent_config("researcher", app_config=app_config)
    assert subagent_skill_registry(resolved, app_config=app_config).available_skills == frozenset()
