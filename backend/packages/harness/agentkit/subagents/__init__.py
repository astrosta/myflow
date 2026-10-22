"""M8 子代理委派。§4 配置、§5 注册表与内置、§6 台账、§7 执行器与隔离循环、§8 结果与
注册表、§9 ``task`` 工具都已经在这里（``runtime`/``capacity` 属 §10）。

**本模块必须是薄门面**：``agentkit.config.subagents_config`` 需要
``agentkit.subagents.config`` 里的默认值（配置层 → subagents 包的依赖边），所以
registry / executor / runtime 这类**反过来 import ``agentkit.config``** 的模块
只能走下面的懒加载，不能在模块顶层 import——否则
``import agentkit.config`` 会在半初始化状态里撞上 ImportError。

要加新的重导出，进 ``_LAZY_EXPORTS``（PEP 562），不要图省事写顶层 import。
"""

from importlib import import_module

from agentkit.subagents.config import (
    DEFAULT_SUBAGENT_DISALLOWED_TOOLS,
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    INHERIT_MODEL,
    SubagentConfig,
    resolve_subagent_model_name,
)

#: 名字 → 定义它的模块。第一次访问才 import。
_LAZY_EXPORTS: dict[str, str] = {
    "BUILTIN_SUBAGENTS": "agentkit.subagents.builtins",
    "BASH_SUBAGENT": "agentkit.subagents.builtins.bash_agent",
    "GENERAL_PURPOSE_SUBAGENT": "agentkit.subagents.builtins.general_purpose",
    "ROUTING_BENEFITS": "agentkit.subagents.routing",
    "ROUTING_COST": "agentkit.subagents.routing",
    "ROUTING_VETOES": "agentkit.subagents.routing",
    "SubagentExecutor": "agentkit.subagents.executor",
    "copy_isolated_subagent_context": "agentkit.subagents.executor",
    "filter_tools": "agentkit.subagents.registry",
    "get_isolated_subagent_loop": "agentkit.subagents.executor",
    "get_subagent_config": "agentkit.subagents.registry",
    "list_subagent_names": "agentkit.subagents.registry",
    "run_on_isolated_subagent_loop": "agentkit.subagents.executor",
    "shutdown_isolated_subagent_loop": "agentkit.subagents.executor",
    "subagent_skill_registry": "agentkit.subagents.registry",
}

__all__ = [
    "BASH_SUBAGENT",
    "BUILTIN_SUBAGENTS",
    "DEFAULT_SUBAGENT_DISALLOWED_TOOLS",
    "DEFAULT_SUBAGENT_MAX_TURNS",
    "DEFAULT_SUBAGENT_TIMEOUT_SECONDS",
    "GENERAL_PURPOSE_SUBAGENT",
    "INHERIT_MODEL",
    "ROUTING_BENEFITS",
    "ROUTING_COST",
    "ROUTING_VETOES",
    "SubagentConfig",
    "SubagentExecutor",
    "copy_isolated_subagent_context",
    "filter_tools",
    "get_isolated_subagent_loop",
    "get_subagent_config",
    "list_subagent_names",
    "resolve_subagent_model_name",
    "run_on_isolated_subagent_loop",
    "shutdown_isolated_subagent_loop",
    "subagent_skill_registry",
]


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path), name)
    globals()[name] = value  # 第二次直接命中
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
