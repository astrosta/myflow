"""Assemble the tool list from config, builtins, and MCP. First name wins."""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from agentkit.config import AppConfig, ToolConfig, get_app_config
from agentkit.reflection import resolve_variable
from agentkit.sandbox import is_host_bash_allowed
from agentkit.sandbox.tools import (
    bash_tool,
    glob,
    grep,
    ls,
    read_file,
    run_code,
    str_replace,
    write_file,
)
from agentkit.tools.builtin.load_skills import load_skills
from agentkit.tools.builtin.search_skills import search_skills
from agentkit.tools.builtin.tool_execute import tool_execute
from agentkit.tools.builtin.tool_search import tool_search

logger = logging.getLogger(__name__)

#: The only external-tool builtins.  ContextForge tools are never bound as
#: LangChain tool objects: tool_search hands the model a card, tool_execute runs
#: it (2026-11-07 design §10).
_CONTEXTFORGE_BUILTIN_TOOL_NAMES = frozenset({"tool_search", "tool_execute"})

_BASH_TOOL_NAMES = frozenset({"bash"})
_HOST_BASH_DISABLED = (
    "Host bash is disabled, so bash tools are omitted. "
    "Switch to a Docker provider, or set sandbox.allow_host_bash: true in a trusted local environment."
)

# (group, tool). Config entries with the same .name win because they are added first.
_BUILTIN_TOOLS: tuple[tuple[str, BaseTool], ...] = (
    # 技能发现只属于 skill-finder；正文加载是框架工具，任何策略下都可用。
    ("skills", search_skills),
    ("skills", load_skills),
    ("sandbox", read_file),
    ("sandbox", write_file),
    ("sandbox", str_replace),
    ("sandbox", ls),
    ("sandbox", glob),
    ("sandbox", grep),
    ("sandbox", bash_tool),
    ("sandbox", run_code),
    ("tools", tool_search),
    ("tools", tool_execute),
)


def get_available_tools(
    groups: list[str] | None = None,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    user_id: str | None = None,
) -> list[BaseTool]:
    """Config tools, then builtins, then MCP. Duplicate ``tool.name`` values are skipped.

    No ContextForge tool object is ever bound: the catalog is reached through the
    two builtins, and ownership is enforced by tool_execute's owner-scoped store
    read, not by a per-user tool table.  ``user_id`` stays in the signature for
    the authorization hook (M9).
    """
    del user_id
    resolved = app_config if app_config is not None else get_app_config()
    selected_groups = set(groups) if groups is not None else None
    tools: list[BaseTool] = []
    seen: set[str] = set()

    for cfg in _configured_tools(resolved):
        if selected_groups is not None and cfg.group not in selected_groups:
            continue
        loaded = resolve_variable(cfg.use, BaseTool)
        if cfg.name != loaded.name:
            logger.warning(
                "Tool name mismatch: config name %r != tool .name %r (use: %s). "
                "The tool's own .name will be used for binding.",
                cfg.name,
                loaded.name,
                cfg.use,
            )
        _add_tool(tools, seen, loaded)

    for group, tool in _BUILTIN_TOOLS:
        if selected_groups is not None and group not in selected_groups:
            continue
        _add_tool(tools, seen, tool)

    return apply_tool_authorization(
        _apply_capability_filters(
            tools,
            app_config=resolved,
            model_name=model_name,
            subagent_enabled=subagent_enabled,
        ),
        app_config=resolved,
    )


def apply_tool_authorization(tools: list[BaseTool], *, app_config: AppConfig | None = None) -> list[BaseTool]:
    """M9 inserts authorization here. M2 returns the list unchanged."""
    del app_config
    return tools


def _configured_tools(app_config: AppConfig) -> list[ToolConfig]:
    raw = getattr(app_config, "tools", None) or []
    configs: list[ToolConfig] = []
    for item in raw:
        if isinstance(item, ToolConfig):
            configs.append(item)
        else:
            configs.append(ToolConfig.model_validate(item))
    return configs


def _add_tool(tools: list[BaseTool], seen: set[str], tool: BaseTool) -> None:
    if tool.name in seen:
        logger.warning("Skipping duplicate tool name %r", tool.name)
        return
    seen.add(tool.name)
    tools.append(tool)


def _apply_capability_filters(
    tools: list[BaseTool],
    *,
    app_config: AppConfig,
    model_name: str | None,
    subagent_enabled: bool,
) -> list[BaseTool]:
    """All capability filters live here. M9 adds non_interactive in this function."""
    bash_allowed = is_host_bash_allowed(app_config)
    # Tolerate a partially-built config (tests hand in plain objects): only the
    # presence of an enabled run_code section turns the tool on.
    run_code_section = getattr(getattr(app_config, "sandbox", None), "run_code", None)
    run_code_enabled = bool(getattr(run_code_section, "enabled", False))
    vision = _model_supports_vision(app_config, model_name)
    # 检索加载关掉时工具不出现：模型看不见它就不会调用，比调用后再回一句"已关闭"省一轮
    skill_search_enabled = bool(getattr(app_config.skills.search, "enabled", True))
    selected: list[BaseTool] = []
    hid_bash = False
    for tool in tools:
        if _is_bash_tool(tool.name) and not bash_allowed:
            hid_bash = True
            continue
        if tool.name == "run_code" and not run_code_enabled:
            continue
        if tool.name == "search_skills" and not skill_search_enabled:
            continue
        if (
            tool.name in _CONTEXTFORGE_BUILTIN_TOOL_NAMES
            and not getattr(app_config.contextforge, "enabled", False)
        ):
            continue
        if tool.name == "view_image" and not vision:
            continue
        if tool.name == "task" and not subagent_enabled:
            continue
        selected.append(tool)
    if hid_bash:
        logger.warning(_HOST_BASH_DISABLED)
    return selected


def _is_bash_tool(name: str) -> bool:
    return name in _BASH_TOOL_NAMES or name.startswith("bash_")


def _model_supports_vision(app_config: AppConfig, model_name: str | None) -> bool:
    if model_name is None:
        return False
    model = app_config.get_model_config(model_name)
    if model is None:
        return False
    return bool(model.supports_vision)