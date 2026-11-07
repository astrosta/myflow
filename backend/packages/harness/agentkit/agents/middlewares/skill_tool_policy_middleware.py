"""把"当前激活的技能"翻译成"本次模型调用能看见哪些工具"（M7 §3.7–§3.9）。

**这是行为约束，不是安全边界。** 模型可以用 bash cat /mnt/skills/xxx/SKILL.md 读全文，
这条路径不会被捕获；自主 skill_context 也有容量上限，老条目会被驱逐。真正的边界是
沙箱与鉴权 —— 把它当"用受限技能隔离不可信用户"是错的。

每次 wrap_model_call：现场解析激活集 → 算允许集合 → 过滤模型可见 schema →
把版本化、JSON-safe、带路径签名的决策写进 secret_context（本步的 tool_call 复用它）。
下一轮总是重算：畸形 / 外来 / 过期 / 对不上的决策一律作废，回退现场解析。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

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

if TYPE_CHECKING:
    from agentkit.skills.catalog import SkillSnapshot

logger = logging.getLogger(__name__)

POLICY_HINT = "请改用其他工具，或者请用户激活一个声明了该工具的技能。"

#: The model-facing ContextForge inner names.  The outer tools are framework
#: tools, but the names inside their arguments/results are business tools and
#: must obey the active skill just like a directly declared tool would.
TOOL_SEARCH_NAME = "tool_search"
TOOL_EXECUTE_NAME = "tool_execute"
INNER_CALL_NAME_KEYS = ("tool_name", "call_name")

#: A tool_search reply whose every card was filtered by the skill policy.  The
#: card list itself is empty; this replaces the "call tool_execute" hint so the
#: model is not told to execute something that is no longer present.
FILTERED_EMPTY_MESSAGE = (
    "当前激活的技能策略不允许本次检索返回的任何 ContextForge 工具。"
    "请改用其他工具，或者请用户激活一个声明了目标 cf__ 工具的技能。"
)


class SkillToolPolicyMiddleware(AgentMiddleware[AgentState]):
    """必须紧挨 SkillActivation 之后注册（§3.9）：它们共享同一个 owner token。"""

    def __init__(
        self,
        *,
        skills_config: SkillsConfig | None = None,
        owner_token: str,
        user_id: str | None = None,
        available_skills: frozenset[str] | None = None,
        snapshot: "SkillSnapshot | None" = None,
    ) -> None:
        super().__init__()
        self._config = skills_config
        self._owner_token = owner_token
        self._user_id = user_id
        self._available_skills = available_skills
        # The snapshot carries the exact container_file used by both slash
        # activation and autonomous skill capture; the policy must index that
        # same generation rather than rescanning a mutable filesystem tree.
        self._snapshot = snapshot

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
            snapshot=self._snapshot,
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
        stored = self._stored_decision(runtime)
        if stored is not None and stored.activated_paths == tuple(sorted(paths)):
            return stored
        return self._compute(paths, slash_path, baseline=baseline, runtime=runtime)

    def _stored_decision(self, runtime) -> ToolPolicyDecision | None:
        """The decision written by this middleware for the current model call.

        No fallback to a fresh computation: inner cf__ names are only safe to
        allow when the exact decision that hid the outer tool table is present.
        """
        return parse_decision(
            read_secret_context(
                getattr(runtime, "context", None),
                SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
                token=self._owner_token,
            )
        )

    def _tool_call_refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        """The outer-tool check, plus tool_execute's inner call_name check."""
        decision = self._decision(
            state=getattr(request, "state", None),
            runtime=request.runtime,
            baseline=frozenset(),
        )
        name = _tool_call_name(request)
        if not decision.is_allowed(name):
            return _policy_error_tool_message(request, decision)
        if name != TOOL_EXECUTE_NAME:
            return None
        inner_name = _inner_tool_name(request)
        stored = self._stored_decision(request.runtime)
        if stored is None:
            return _inner_policy_error_tool_message(
                request, inner_name=inner_name, decision=None
            )
        if not stored.is_allowed(inner_name):
            return _inner_policy_error_tool_message(
                request, inner_name=inner_name, decision=stored
            )
        return None

    def _filter_tool_search_result(self, request: ToolCallRequest, result: Any) -> Any:
        """Drop cards the active skill may not execute from a tool_search reply.

        The result is a Command carrying a ToolMessage and the run-scoped
        retrieved names.  Both have to be filtered together, otherwise a name
        removed from the model-visible cards could still pass tool_execute's
        retrieval gate.  A missing decision filters everything: fail closed.
        """
        decision = self._stored_decision(request.runtime)
        if decision is not None and decision.unlimited:
            return result
        if isinstance(result, ToolMessage):
            return _filter_tool_search_summary(result, decision)
        if not isinstance(result, Command):
            return result
        update = result.update
        if not isinstance(update, Mapping):
            return result

        new_update = dict(update)
        changed = False
        messages = update.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            filtered_messages = []
            messages_changed = False
            for message in messages:
                filtered = _filter_tool_search_summary(message, decision)
                if filtered is not message:
                    messages_changed = True
                filtered_messages.append(filtered)
            if messages_changed:
                new_update["messages"] = filtered_messages
                changed = True

        if "retrieved_tool_names" in update:
            names = update["retrieved_tool_names"]
            filtered_names = _filter_retrieved_names(names, decision)
            if filtered_names is not names:
                new_update["retrieved_tool_names"] = filtered_names
                changed = True

        return replace(result, update=new_update) if changed else result

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
        refusal = self._tool_call_refusal(request)
        if refusal is not None:
            return refusal
        result = handler(request)
        if _tool_call_name(request) == TOOL_SEARCH_NAME:
            return self._filter_tool_search_result(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        refusal = self._tool_call_refusal(request)
        if refusal is not None:
            return refusal
        result = await handler(request)
        if _tool_call_name(request) == TOOL_SEARCH_NAME:
            return self._filter_tool_search_result(request, result)
        return result


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


def _inner_tool_name(request: ToolCallRequest) -> str:
    """tool_execute's model-facing inner name (the cf__ call name)."""
    tool_call = getattr(request, "tool_call", None) or {}
    args = tool_call.get("args") if isinstance(tool_call, Mapping) else None
    if not isinstance(args, Mapping):
        return ""
    for key in INNER_CALL_NAME_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _filter_tool_search_summary(message: Any, decision: ToolPolicyDecision | None) -> Any:
    """Filter one tool_search ToolMessage's JSON cards, leaving other messages alone."""
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return message
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return message
    if not isinstance(payload, Mapping):
        return message
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return message
    kept = [
        card
        for card in tools
        if isinstance(card, Mapping) and _name_allowed(decision, _card_call_name(card))
    ]
    if len(kept) == len(tools):
        return message
    filtered = dict(payload)
    filtered["tools"] = kept
    if not kept:
        filtered["message"] = FILTERED_EMPTY_MESSAGE
    return message.model_copy(
        update={"content": json.dumps(filtered, ensure_ascii=False)}
    )


def _filter_retrieved_names(names: Any, decision: ToolPolicyDecision | None) -> Any:
    if not isinstance(names, list):
        return names
    kept = [
        name
        for name in names
        if isinstance(name, str) and _name_allowed(decision, name)
    ]
    return kept if len(kept) != len(names) else names


def _card_call_name(card: Mapping) -> str:
    name = card.get("call_name")
    return name.strip() if isinstance(name, str) else ""


def _name_allowed(decision: ToolPolicyDecision | None, name: str) -> bool:
    return bool(name) and decision is not None and decision.is_allowed(name)


def _inner_policy_error_tool_message(
    request: ToolCallRequest,
    *,
    inner_name: str,
    decision: ToolPolicyDecision | None,
) -> ToolMessage:
    """Refuse a tool_execute whose inner cf__ name the active skill may not run."""
    tool_call = getattr(request, "tool_call", None) or {}
    name = _tool_call_name(request) or TOOL_EXECUTE_NAME
    tool_call_id = (
        str(tool_call.get("id") or "missing_tool_call_id")
        if isinstance(tool_call, Mapping)
        else "missing_tool_call_id"
    )
    inner = inner_name or "<missing>"
    if decision is None:
        content = (
            f"错误：当前激活的技能策略决策缺失，已拒绝执行 ContextForge 工具 '{inner}'"
            f"（fail closed）。{POLICY_HINT}"
        )
    else:
        content = (
            f"错误：当前激活的技能下不可执行 ContextForge 工具 '{inner}'"
            f"（策略模式：{decision.mode}）。{POLICY_HINT}"
        )
    message = ToolMessage(
        content=content, tool_call_id=tool_call_id, name=name, status="error"
    )
    return normalize_tool_result(message, tool_call_id=tool_call_id)


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
