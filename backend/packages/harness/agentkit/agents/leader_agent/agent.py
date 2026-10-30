from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
from agentkit.models.catalog import split_model_name
from agentkit.runtime.user_context import resolve_runtime_user_id
from agentkit.tools import get_available_tools

from agentkit.subagents.policy import SubagentPolicy

if TYPE_CHECKING:  # 运行时在 assemble_agent 里 import：subagents 那条链会绕回本模块
    from agentkit.subagents.runtime import SubagentRuntime

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
    personalization_digest: str = ""

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, AgentCacheKey)
            and self.assembly_inputs == other.assembly_inputs
            and self.user_id == other.user_id
            and self.app_config is other.app_config
            and self.checkpointer is other.checkpointer
            and self.personalization_digest == other.personalization_digest
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.assembly_inputs,
                self.user_id,
                id(self.app_config),
                id(self.checkpointer),
                self.personalization_digest,
            )
        )


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
        personalization_digest=_personalization_digest(app_config),
    )


def _personalization_digest(app_config: AppConfig) -> str:
    if not app_config.memory.openviking.enabled:
        return ""
    personalization = app_config.memory.openviking.personalization
    if not personalization.enabled:
        return ""
    from agentkit.agents.memory.openviking.profile import read_assistant_personalization

    return read_assistant_personalization(
        identity_path=personalization.identity_file,
        soul_path=personalization.soul_file,
    ).digest

def make_agent(config: RunnableConfig):
    return assemble_agent(config).graph

def assemble_agent(
    config: RunnableConfig,
    *,
    app_config: AppConfig | None = None, 
    checkpointer: Checkpointer | None = None,
    subagent_runtime: SubagentRuntime | None = None,
) -> AgentAssembly:

    cfg = (config or {}).get("configurable", {})
    resolved_config = app_config or get_app_config()

    user_id = _resolve_user_id(config)
    requested_model  = _resolve_runtime_option(cfg, "model_name", None, resolved_config.agents.default_model)
    thinking_enabled = _resolve_runtime_option(cfg, "thinking_enabled", None, False)
    requested_effort = _resolve_runtime_option(cfg, "reasoning_effort", None, None)
    subagent_enabled = bool(_resolve_runtime_option(cfg, "subagent_enabled", None, False))

    # 能力协商
    model_name = _effective_model_name(requested_model, resolved_config)
    thinking_enabled = _negotiate_thinking(model_name, thinking_enabled, resolved_config)
    reasoning_effort = _negotiate_reasoning_effort(model_name, requested_effort, resolved_config)

    # `<name>@<vendor-id>`：厂商目录里新冒出来的模型，复用 name 那条配置，只换 provider 侧模型名
    model_entry, vendor_model = split_model_name(model_name)
    overrides = {"model": vendor_model} if vendor_model else None

    # 建立模型
    model = create_chat_model(
        name=model_entry,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        app_config=resolved_config,
        model_overrides=overrides,
        attach_tracing=False,
    )

    # M8 §10.1：进程级共享的容量控制器。**同一个实例**要喂给 task 闭包、Limit 中间件、执行器，
    # 否则每个 run 一套各 3 槽的控制器，上限形同虚设。
    runtime = subagent_runtime
    if subagent_enabled and runtime is None:
        from agentkit.subagents.runtime import get_subagent_runtime

        runtime = get_subagent_runtime(resolved_config)

    # M8 §16：委派策略在 run 开始时**快照一次**，写进 run config 的 context。
    # task 工具（执行侧）与 Limit 中间件（剥调用）之后都读这一份，运行中改 YAML 不影响
    # 本次 run——否则同一次 run 的前后两次委派会用两套策略。
    policy = None
    if subagent_enabled:
        from agentkit.subagents.policy import snapshot_subagent_policy

        policy = snapshot_subagent_policy(
            resolved_config,
            max_running=runtime.config.max_running if runtime is not None else None,
        )
        _stamp_subagent_policy(config, policy)

    # 组装工具
    tools = get_available_tools(
        model_name=model_name,
        subagent_enabled=subagent_enabled,
        app_config=resolved_config,
    )
    if runtime is not None and policy is not None and policy.enabled:
        # task 是**闭包**，不是内置工具表里的静态对象：它要绑到那个共享 runtime 上。
        # §16：[]（硬拒绝）时连工具都不装——看不见也调不动。
        from agentkit.tools.builtin.task_tool import make_task_tool

        tools = [*tools, make_task_tool(dispatcher=runtime, app_config=resolved_config)]
    if resolved_config.memory.openviking.enabled:
        from agentkit.agents.memory.openviking.runtime import get_openviking_memory_runtime

        runtime = get_openviking_memory_runtime()
        if runtime is not None:
            existing_names = {tool.name for tool in tools}
            tools = [*tools, *(tool for tool in runtime.tools if tool.name not in existing_names)]
        else:
            logger.warning(
                "memory.openviking.enabled=true but the OpenViking runtime is not initialized"
            )
    if resolved_config.knowledge_base.enabled:
        # Identity is read from runtime.context inside the tool, never from model args.
        from agentkit.knowledge.tool import TOOL_NAME as KNOWLEDGE_TOOL_NAME
        from agentkit.knowledge.tool import make_knowledge_search_tool

        if KNOWLEDGE_TOOL_NAME not in {tool.name for tool in tools}:
            tools = [*tools, make_knowledge_search_tool()]
    # 过滤（将来 M9 的鉴权也在这里）必须看到**完整**的工具表，所以 task 在它之前就位
    tools = apply_tool_filter(tools, cfg=cfg)

    # 组中间件（M6 §3.2 的顺序；M7 §3.9 的技能槽位在 assembly 里）
    middleware = build_middlewares(
        config, app_config=resolved_config, user_id=user_id, subagent_policy=policy
    )

    # 渲染系统提示词（§16：委派段读的是同一份快照）
    system_prompt = apply_prompt_template(
        agent_name=_resolve_runtime_option(cfg, "agent_name", None, None),
        subagent_enabled=subagent_enabled,
        app_config=resolved_config,
        user_id=user_id,
        subagent_policy=policy,
    )

    graph = _compile_agent(model=model, tools=tools, middleware=middleware, system_prompt=system_prompt, state_schema=ThreadState, checkpointer=checkpointer)
    return AgentAssembly(graph=graph, descriptor=None)

@dataclass(frozen=True)
class AgentAssembly:
    graph: Any
    descriptor: Any | None

def unwrap_agent_graph(result: Any) -> Any:
    return result.graph if isinstance(result, AgentAssembly) else result


def _stamp_subagent_policy(config: RunnableConfig, policy: SubagentPolicy) -> None:
    """把策略快照写进 run config 的 ``context``（worker 会把它变成 runtime.context）。

    这两个键**客户端够不到**：``build_run_config`` 只从服务端塞 context，而
    ``normalize_client_configurable`` 会删掉所有非装配键。所以这里写的是服务端权威值。
    """
    if not isinstance(config, dict):
        return
    context = config.get("context")
    if not isinstance(context, dict):
        context = {}
        config["context"] = context
    context.update(policy.to_context())

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
    if requested is not None:
        entry, _vendor = split_model_name(requested)
        if app_config.get_model_config(entry) is not None:
            return requested
    
    if requested is not None:
        logger.warning(f"Model {requested} not found in config, using default model")

    if app_config.agents.default_model is not None:
        return app_config.agents.default_model
    
    
    return app_config.models[0].name
    


def _negotiate_thinking(model_name: str, thinking_enabled: bool, app_config: AppConfig) -> bool:
    if not thinking_enabled:
        return False
    model_config = app_config.get_model_config(split_model_name(model_name)[0])
    if model_config is None or not model_config.supports_thinking:
        logger.warning(f"Model {model_name} does not support thinking, setting thinking_enabled to False")
        return False
    return thinking_enabled


def _negotiate_reasoning_effort(
    model_name: str, reasoning_effort: str | None, app_config: AppConfig
) -> str | None:
    """配置里公布的等级才允许下发；旧客户端带着过期等级来时回退到模型默认。"""

    if reasoning_effort is None:
        return None
    model_config = app_config.get_model_config(split_model_name(model_name)[0])
    if model_config is None or not model_config.supports_reasoning_effort:
        logger.warning(f"Model {model_name} does not support reasoning effort, ignoring {reasoning_effort!r}")
        return None
    published = model_config.reasoning_effort_ids()
    if published and reasoning_effort not in published:
        logger.warning(
            f"Model {model_name} does not publish reasoning effort {reasoning_effort!r}; "
            f"published: {published}; using the provider default"
        )
        return None
    return reasoning_effort


def apply_tool_filter(tools: list, cfg: dict) -> list:
    return list(tools)

def build_middlewares(
    config: RunnableConfig,
    *,
    app_config: AppConfig | None = None,
    user_id: str | None = None,
    subagent_policy: SubagentPolicy | None = None,
) -> list:
    """lead 的完整中间件链。顺序与槽位注释都在 middlewares/assembly.py。"""
    from agentkit.agents.middlewares.assembly import build_middlewares as _assemble

    resolved = app_config or get_app_config()
    cfg = (config or {}).get("configurable", {})
    model_name = _resolve_runtime_option(cfg, "model_name", None, resolved.agents.default_model)
    return _assemble(
        config,
        model_name,
        app_config=resolved,
        user_id=user_id,
        subagent_policy=subagent_policy,
    )


def _resolve_user_id(config: RunnableConfig) -> str | None:
    """user_id 是服务端注入的：只从 context 读，不从 configurable 读（M6 §4.1）。

    走 M9 §2.1 的**同一条**优先级链（context → 请求 ContextVar），
    否则会出现"图构建和中间件落进不同用户桶"——多用户下才现形的那类 bug。
    都没有时返回 None：M7 的 skills_prefs 用 None 表示 `__default__` 桶。
    """
    context = (config or {}).get("context")
    return resolve_runtime_user_id(context if isinstance(context, Mapping) else None)

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