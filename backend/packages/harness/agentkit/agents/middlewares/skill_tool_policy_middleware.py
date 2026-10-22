"""把"当前激活的技能"翻译成"本次模型调用能看见哪些工具"（M7 §3.7–§3.9）。

**这是行为约束，不是安全边界。** 模型可以用 bash cat /mnt/skills/xxx/SKILL.md 读全文，
这条路径不会被捕获；自主 skill_context 也有容量上限，老条目会被驱逐。真正的边界是
沙箱与鉴权 —— 把它当"用受限技能隔离不可信用户"是错的。

每次 wrap_model_call：现场解析激活集 → 算允许集合 → 过滤模型可见 schema →
把版本化、JSON-safe、带路径签名的决策写进 secret_context（本步的 tool_call 复用它）。
下一轮总是重算：畸形 / 外来 / 过期 / 对不上的决策一律作废，回退现场解析。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agentkit.agents.middlewares.tool_result_meta import normalize_tool_result
from agentkit.config.skills_config import SkillsConfig
from agentkit.runtime.secret_context import (
    SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
    SLASH_SKILL_SOURCE_CONTEXT_KEY,
    read_secret_context,
    write_secret_context,
)
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.tool_policy import (
    ToolPolicyDecision,
    build_decision,
    framework_only_decision,
    parse_decision,
)
from agentkit.skills.types import Skill

logger = logging.getLogger(__name__)

POLICY_HINT = "请改用其他工具，或者请用户激活一个声明了该工具的技能。"


class SkillToolPolicyMiddleware(AgentMiddleware[AgentState]):
    """必须紧挨 SkillActivation 之后注册（§3.9）：它们共享同一个 owner token。"""

    def __init__(
        self,
        *,
        skills_config: SkillsConfig,
        owner_token: str,
        user_id: str | None = None,
        available_skills: frozenset[str] | None = None,
        prefs_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._config = skills_config
        self._owner_token = owner_token
        self._user_id = user_id
        self._available_skills = available_skills
        self._prefs_path = prefs_path

    # ---- 现场解析 ----------------------------------------------------------

    def _runtime_user_id(self, runtime) -> str | None:
        context = getattr(runtime, "context", None)
        if isinstance(context, Mapping):
            value = context.get("user_id")
            if isinstance(value, str) and value:
                return value
        return self._user_id

    def _index(self, runtime) -> dict[str, Skill]:
        """规范化容器路径 → 技能。加载失败或用户关掉之后这里就没有它（fail-closed）。"""
        registry = SkillRegistry(
            config=self._config,
            user_id=self._runtime_user_id(runtime),
            available_skills=self._available_skills,
            prefs_path=self._prefs_path,
        )
        return registry.index_by_container_file()

    def _activation_paths(self, state: Mapping | None, runtime) -> tuple[list[str], str | None]:
        """slash 源只在持有同一 token 时才算数（§3.6）。"""
        context = getattr(runtime, "context", None)
        slash_path = read_secret_context(
            context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=self._owner_token
        )
        slash = slash_path if isinstance(slash_path, str) and slash_path else None
        paths: list[str] = []
        if slash:
            paths.append(slash)
        for entry in (state or {}).get("skill_context") or []:
            if isinstance(entry, Mapping):
                path = entry.get("path")
                if isinstance(path, str) and path and path not in paths:
                    paths.append(path)
        return paths, slash

    def _compute(
        self,
        paths: Sequence[str],
        slash_path: str | None,
        *,
        baseline: frozenset[str],
        runtime: Any,
    ) -> ToolPolicyDecision:
        index = self._index(runtime)
        activated = [skill for path in paths if (skill := index.get(path)) is not None]
        slash_skill = index.get(slash_path) if slash_path else None
        if paths and not activated:
            # 激活集非空但 0 个能解析：fail-closed 到框架工具（§3.9 失败语义）
            logger.warning("No activated skill resolved (%s); failing closed", list(paths))
            return framework_only_decision(reason="unresolved")
        return build_decision(baseline=baseline, activated=activated, slash_skill=slash_skill)

    def _decision(
        self, *, state: Mapping | None, runtime, baseline: frozenset[str]
    ) -> ToolPolicyDecision:
        paths, slash_path = self._activation_paths(state, runtime)
        context = getattr(runtime, "context", None)
        stored = parse_decision(
            read_secret_context(
                context, SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY, token=self._owner_token
            )
        )
        if stored is not None and stored.activated_paths == tuple(sorted(paths)):
            return stored
        return self._compute(paths, slash_path, baseline=baseline, runtime=runtime)

    # ---- hooks -------------------------------------------------------------

    def _inject(self, request: ModelRequest) -> ModelRequest:
        tools = list(getattr(request, "tools", None) or [])
        baseline = frozenset(name for tool in tools if (name := _tool_name(tool)))
        decision = self._decision(state=request.state, runtime=request.runtime, baseline=baseline)
        write_secret_context(
            getattr(request.runtime, "context", None),
            SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
            decision.to_payload(),
            token=self._owner_token,
        )
        if decision.unlimited:
            return request
        visible = [tool for tool in tools if decision.is_allowed(_tool_name(tool) or "")]
        return request.override(tools=visible)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._inject(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._inject(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        decision = self._decision(
            state=getattr(request, "state", None), runtime=request.runtime, baseline=frozenset()
        )
        name = _tool_call_name(request)
        if not decision.is_allowed(name):
            return _policy_error_tool_message(request, decision)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        decision = self._decision(
            state=getattr(request, "state", None), runtime=request.runtime, baseline=frozenset()
        )
        name = _tool_call_name(request)
        if not decision.is_allowed(name):
            return _policy_error_tool_message(request, decision)
        return await handler(request)


def _tool_name(tool: Any) -> str | None:
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool, Mapping):
        candidate = tool.get("name")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _tool_call_name(request: ToolCallRequest) -> str:
    tool_call = getattr(request, "tool_call", None) or {}
    name = tool_call.get("name") if isinstance(tool_call, Mapping) else None
    return str(name) if name else ""


def _policy_error_tool_message(request: ToolCallRequest, decision: ToolPolicyDecision) -> ToolMessage:
    """短路成一条错误 ToolMessage：模型看到"这个工具在当前技能下不可用，换个工具"。

    走 normalize_tool_result 盖章（myflow_tool_meta，recoverable_by_model=True，
    建议换工具），而不是抛异常让整轮失败。
    """
    tool_call = getattr(request, "tool_call", None) or {}
    name = _tool_call_name(request) or "unknown_tool"
    tool_call_id = str(tool_call.get("id") or "missing_tool_call_id") if isinstance(tool_call, Mapping) else "missing_tool_call_id"
    content = (
        f"错误：当前激活的技能下不可使用工具 '{name}'"
        f"（策略模式：{decision.mode}）。{POLICY_HINT}"
    )
    message = ToolMessage(content=content, tool_call_id=tool_call_id, name=name, status="error")
    return normalize_tool_result(message, tool_call_id=tool_call_id)
