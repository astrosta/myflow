"""子代理注册表（M8 §5，D2）。

解析顺序（§5.1）：

    内置 BUILTIN_SUBAGENTS
      → config.yaml subagents.custom_agents[name]     # 同名覆盖内置（打 warning）
      → config.yaml subagents.agents[name]            # 超时 / 轮次 / 模型 / token_budget

M8 **不做**管理员托管定义表（``persistence/managed_subagents/`` 那种形态）：
部署级自定义走 ``custom_agents`` 就够，托管存储跟 Custom Agent 同一套 backend。

三条容易踩的纪律：

* **发现与执行必须是同一个答案**：``list_subagent_names`` 和 ``get_subagent_config``
  共用同一套可用性判断。只过滤一边的后果很具体——模型看得见却调不动（白烧轮次），
  或者看不见却能调（策略形同虚设）。
* **段落值只填内置**：内置的墙钟来自 ``subagents.timeout_seconds``，轮次在
  ``subagents.max_turns`` 非 null 时被它覆盖；custom 类型带**自己的**值（§4.1）。
  ``agents.<name>`` 永远最后生效。
* **过滤发生在 dispatch 那一刻**：``filter_tools`` 吃的是 lead 当时持有的工具列表。
  子代理循环里**不要**再调一次 ``get_available_tools()``——那会看到热重载后的另一代
  MCP 工具，和这次委派开始时承诺的工具集对不上（§5.3）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from langchain_core.tools import BaseTool

from agentkit.config import AppConfig
from agentkit.config.subagents_config import SubagentOverrideConfig, SubagentsConfig
from agentkit.runtime.user_context import get_effective_user_id
from agentkit.sandbox.security import is_host_bash_allowed
from agentkit.skills.storage import SkillRegistry
from agentkit.subagents.builtins import BUILTIN_SUBAGENTS
from agentkit.subagents.builtins import bash_agent as _bash_agent
from agentkit.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)

#: 唯一一个"默认可能不可用"的内置类型（§5.2）：宿主 bash 没开时它整条消失。
BASH_SUBAGENT_NAME = _bash_agent.NAME


def get_subagent_config(name: str, *, app_config: AppConfig) -> SubagentConfig | None:
    """解析一个子代理类型。未知、或在当前沙箱下不可用 → None（§5.1）。

    ``None`` 是**正常路径**：``task`` 工具把它翻译成一条列出"可用类型是 …"的工具结果，
    而不是抛异常（子代理失败是"这次委派的结果"，不是父 run 的错误）。
    """
    builtin = BUILTIN_SUBAGENTS.get(name)
    custom = _from_custom_agents(name, app_config)
    from_builtin = custom is None
    base = custom if custom is not None else builtin
    if base is None:
        return None
    if from_builtin and not _builtin_available(name, app_config):
        return None
    return _apply_overrides(
        base,
        app_config.subagents.agents.get(name),
        app_config.subagents,
        from_builtin=from_builtin,
    )


def list_subagent_names(*, app_config: AppConfig, allowed: list[str] | None) -> list[str]:
    """发现列表：提示词与 ``task`` 执行**共用**这一份（§5.1 / §16）。

    ``allowed``：``None`` = 全部，``[]`` = 硬拒绝（提示词里不出现委派段，``task`` 也
    可以从 lead 工具集里拿掉），列表 = 白名单。

    调用方要在 **run 开始时**把这份策略快照进 run 元数据 / ``runtime.context``，
    之后不要重读可变的 agent 配置：否则同一次 run 的前后两次委派会用不同的策略。
    """
    names = [*_discoverable_builtin_names(app_config), *app_config.subagents.custom_agents]
    names = list(dict.fromkeys(names))  # custom_agents 同名覆盖内置时只能出现一次
    if allowed is None:
        return names
    if allowed == []:
        return []
    allowed_set = set(allowed)
    return [name for name in names if name in allowed_set]


def filter_tools(
    all_tools: Sequence[BaseTool],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[BaseTool]:
    """按类型声明过滤工具（§5.3）。

    ``allowed=None`` = 继承 lead 当时持有的全部工具；``disallowed`` 最后扣。
    两个名单都按 ``tool.name`` 匹配："有则留，无则跳过"——声明了 ``bash`` 但当前
    lead 根本没有这个工具时，少一个工具比多一个坏工具安全。
    """
    filtered = list(all_tools)
    if allowed is not None:
        wanted = set(allowed)
        filtered = [tool for tool in filtered if tool.name in wanted]
    if disallowed is not None:
        denied = set(disallowed)
        filtered = [tool for tool in filtered if tool.name not in denied]
    return filtered


def subagent_skill_registry(
    config: SubagentConfig,
    *,
    app_config: AppConfig,
    user_id: str | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> SkillRegistry:
    """子代理解析技能时用的注册表：**跟父身份走**（§5.4）。

    用父 runtime 的 ``user_id``，没有身份才退到 ``DEFAULT_USER_ID``。不要读全局-only
    catalog：技能有 public → custom 的 shadowing，也有用户自己的启用开关，用错身份会让
    子代理看见 lead 看不见的技能（或反过来），排查时只看得到"技能时有时无"。

    ``config.skills``：``None`` = 全部启用技能，``[]`` = 禁用技能（不是"继承 lead 的"）。
    """
    return SkillRegistry(
        config=app_config.skills,
        user_id=user_id or get_effective_user_id(runtime_context),
        available_skills=None if config.skills is None else frozenset(config.skills),
    )


def _builtin_available(name: str, app_config: AppConfig) -> bool:
    """§5.2：宿主 bash 未允许且当前沙箱不是隔离 provider 时，``bash`` 整条不进发现列表。

    ``is_host_bash_allowed`` 对隔离 provider（Docker 等）恒为 True——那种沙箱里跑 shell
    本来就是设计；本地 provider 才看 ``sandbox.allow_host_bash``。
    """
    if name != BASH_SUBAGENT_NAME:
        return True
    return is_host_bash_allowed(app_config)


def _discoverable_builtin_names(app_config: AppConfig) -> list[str]:
    """内置的发现顺序（§5.2）。同名覆盖交给 ``list_subagent_names`` 的去重处理：
    在这里提前剔掉会把被覆盖的类型挤到列表末尾，发现顺序就不稳定了。"""
    return [name for name in BUILTIN_SUBAGENTS if _builtin_available(name, app_config)]


def _from_custom_agents(name: str, app_config: AppConfig) -> SubagentConfig | None:
    """把 YAML 的 custom_agents 条目翻成 dataclass。同名时**后写的 YAML 赢**（§5.1）。"""
    raw = app_config.subagents.custom_agents.get(name)
    if raw is None:
        return None
    if name in BUILTIN_SUBAGENTS:
        logger.warning("subagents.custom_agents.%s overrides the builtin subagent", name)
    return SubagentConfig(
        name=name,
        description=raw.description,
        system_prompt=raw.system_prompt,
        tools=None if raw.tools is None else list(raw.tools),
        disallowed_tools=list(raw.disallowed_tools),
        skills=None if raw.skills is None else list(raw.skills),
        model=raw.model,
        max_turns=raw.max_turns,
        timeout_seconds=raw.timeout_seconds,
    )


def _apply_overrides(
    base: SubagentConfig,
    override: SubagentOverrideConfig | None,
    section: SubagentsConfig,
    *,
    from_builtin: bool,
) -> SubagentConfig:
    """段落值（只对内置）→ ``agents.<name>`` 覆盖，顺序不能反（§4.1 / §5.1）。

    ``dataclasses.replace`` 复制而不是就地改：``BUILTIN_SUBAGENTS`` 是模板，
    被这一次委派改脏了，下一个用户就拿到别人的超时和工具集。列表字段也重建一份。
    """
    max_turns = base.max_turns
    timeout_seconds = base.timeout_seconds
    if from_builtin:
        timeout_seconds = section.timeout_seconds
        if section.max_turns is not None:
            max_turns = section.max_turns
    model = base.model
    if override is not None:
        if override.timeout_seconds is not None:
            timeout_seconds = override.timeout_seconds
        if override.max_turns is not None:
            max_turns = override.max_turns
        if override.model is not None:
            model = override.model
    return replace(
        base,
        tools=None if base.tools is None else list(base.tools),
        disallowed_tools=None if base.disallowed_tools is None else list(base.disallowed_tools),
        skills=None if base.skills is None else list(base.skills),
        model=model,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "BASH_SUBAGENT_NAME",
    "filter_tools",
    "get_subagent_config",
    "list_subagent_names",
    "subagent_skill_registry",
]
