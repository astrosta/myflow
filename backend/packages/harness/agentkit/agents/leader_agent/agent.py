from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langgraph.types import Checkpointer

from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.agents.leader_agent.runtime_keys import (
    ASSEMBLY_INPUT_KEYS,
    CLIENT_ASSEMBLY_KEYS,
)
from agentkit.agents.thread_state import ThreadState
from agentkit.config import AppConfig, get_app_config
from agentkit.models import create_chat_model
from agentkit.tools import get_available_tools

logger = logging.getLogger(__name__)

_MISSING = object()


@dataclass(frozen=True, eq=False)
class AgentCacheKey:
    """装配键和 user_id 按值比；app_config 和 checkpointer 按对象身份比。

    键持有两者的引用：键活着，旧对象就不会被回收，__hash__ 里的 id 也就不会被新对象复用。
    """

    assembly_inputs: tuple[tuple[str, Any], ...]
    user_id: str
    app_config: AppConfig
    checkpointer: Any

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, AgentCacheKey)
            and self.assembly_inputs == other.assembly_inputs
            and self.user_id == other.user_id
            and self.app_config is other.app_config
            and self.checkpointer is other.checkpointer
        )

    def __hash__(self) -> int:
        return hash((self.assembly_inputs, self.user_id, id(self.app_config), id(self.checkpointer)))


def agent_cache_key(
    cfg: Mapping[str, Any],
    *,
    app_config: AppConfig,
    checkpointer: Any,
    user_id: str,
) -> AgentCacheKey:
    return AgentCacheKey(
        assembly_inputs=tuple((key, cfg.get(key, _MISSING)) for key in sorted(ASSEMBLY_INPUT_KEYS)),
        user_id=user_id,
        app_config=app_config,
        checkpointer=checkpointer,
    )

def make_agent(config: RunnableConfig):
    return assemble_agent(config).graph

def assemble_agent(
    config: RunnableConfig,
    *,
    app_config: AppConfig | None = None, 
    checkpointer: Checkpointer | None = None,
) -> AgentAssembly:

    cfg = (config or {}).get("configurable", {})
    resolved_config = app_config or get_app_config()

    requested_model  = _resolve_runtime_option(cfg, "model_name", None, resolved_config.agents.default_model)
    thinking_enabled = _resolve_runtime_option(cfg, "thinking_enabled", None, False)

    # 能力协商
    model_name = _effective_model_name(requested_model, resolved_config)
    thinking_enabled = _negotiate_thinking(model_name, thinking_enabled, resolved_config)

    # 建立模型
    model = create_chat_model(name=model_name, thinking_enabled=thinking_enabled, app_config=resolved_config, attach_tracing=False)

    # 组装工具
    tools = apply_tool_filter(
        get_available_tools(
            model_name=model_name,
            subagent_enabled=bool(_resolve_runtime_option(cfg, "subagent_enabled", None, False)),
            app_config=resolved_config,
        ),
        cfg=cfg,
    )

    # 组中间件
    middleware = build_middlewares(config, app_config=resolved_config)

    # 渲染系统提示词
    system_prompt = apply_prompt_template(
        agent_name=_resolve_runtime_option(cfg, "agent_name", None, None),
        subagent_enabled=_resolve_runtime_option(cfg, "subagent_enabled", None, False),
        app_config=resolved_config,
    )

    graph = _compile_agent(model=model, tools=tools, middleware=middleware, system_prompt=system_prompt, state_schema=ThreadState, checkpointer=checkpointer)
    return AgentAssembly(graph=graph, descriptor=None)

@dataclass(frozen=True)
class AgentAssembly:
    graph: Any
    descriptor: Any | None

def unwrap_agent_graph(result: Any) -> Any:
    return result.graph if isinstance(result, AgentAssembly) else result

def _resolve_runtime_option(cfg: dict, key: str, agent_value, default):
    """Resolve a runtime option with ``request > agent config > default`` precedence.
    ...
    """
    if key in cfg:
        return cfg[key]
    if agent_value is not None:
        return agent_value
    return default

def _effective_model_name(requested: str | None, app_config: AppConfig) -> str:
    """请求里的名字能在 models 里解析到，就用它。
    解析不到：告警，回退 agents.default_model。
    default_model 也没有：回退 models[0].name，与模型工厂的缺省规则一致。
    回退之后仍然建不出模型，那是配置错误，让 create_chat_model 抛。"""
    if requested is not None and app_config.get_model_config(requested) is not None:
        return requested
    
    if requested is not None:
        logger.warning(f"Model {requested} not found in config, using default model")

    if app_config.agents.default_model is not None:
        return app_config.agents.default_model
    
    
    return app_config.models[0].name
    


def _negotiate_thinking(model_name: str, thinking_enabled: bool, app_config: AppConfig) -> bool:
    if thinking_enabled and not app_config.models.get(model_name).supports_thinking:
        logger.warning(f"Model {model_name} does not support thinking, setting thinking_enabled to False")
        return False
    return thinking_enabled


def apply_tool_filter(tools: list, cfg: dict) -> list:
    return list(tools)

def build_middlewares(config: RunnableConfig, *, app_config: AppConfig | None = None) -> list:
    return []

def _compile_agent(*, model, tools, middleware, system_prompt, state_schema, checkpointer):
    kwargs = dict(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=state_schema,
    )

    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    
    return create_agent(**kwargs)


def normalize_client_configurable(cfg: dict) -> dict:
    """只保留 CLIENT_ASSEMBLY_KEYS。不在这里补服务端的权威值。

    服务端键、身份别名、checkpoint 坐标、context-only 键和 __ 前缀都会被删掉。
    M1 的 make_agent / assemble_agent 不调用这个函数。
    """
    cleaned = {
        key: value
        for key, value in cfg.items()
        if key in CLIENT_ASSEMBLY_KEYS
    }
    return cleaned