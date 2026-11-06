"""Model tools backed by the OpenViking memory adapter.

The retrieval tools stay read-only. The one write-shaped tool,
`memory_remember`, does not write OpenViking itself: it marks the current
turn's decision so the asynchronous capture worker commits the turn after it
has appended the conversation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, replace
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from agentkit.agents.memory.openviking.config import DEFAULT_MEMORY_TYPES as ALLOWED_MEMORY_TYPES
from agentkit.agents.memory.openviking.contracts import MemoryHit, TurnDecision
from agentkit.runtime.user_context import resolve_runtime_user_id

DEFAULT_SEARCH_TYPES = ["preferences", "entities", "events"]
MAX_LIMIT = 20
MAX_READ_CHARS = 16_000
EXPLICIT_REMEMBER_TOOL_NAME = "memory_remember"
#: Alias matching the single-write-tool convention used by profile_tools.
TOOL_NAME = EXPLICIT_REMEMBER_TOOL_NAME

MemoryTypeArg = Literal[
    "preferences",
    "entities",
    "events",
    "cases",
    "trajectories",
    "experiences",
    "tools",
]


class MemorySearchInput(BaseModel):
    query: str = Field(description="要回忆的事实，写成自包含的一句话。")
    memory_types: list[MemoryTypeArg] | None = Field(
        default=None,
        description=(
            "按需选择本次要查的类型，只传和问题相关的，一次一类；不要把全部类型一起传，也不要省略。"
            "preferences：稳定偏好与习惯。"
            "entities：人、项目、组织、产品的稳定卡片。"
            "events：发生过的事件、决定、承诺；只有查这类才能用 since/until。"
            "cases：可评估的任务案例。"
            "trajectories：可复用的操作步骤。"
            "experiences：从成功或失败提炼的经验。"
            "tools：工具的用法、参数和失败模式。"
        ),
    )
    limit: int = Field(default=8, description="最多返回几条，1–20。")
    since: str | None = Field(
        default=None,
        description=(
            "ISO-8601 时间下界（如 2026-10-01 或 2026-10-01T00:00:00Z）。"
            "仅当 memory_types 为 ['events'] 时有效。"
        ),
    )
    until: str | None = Field(
        default=None,
        description="ISO-8601 时间上界。仅当 memory_types 为 ['events'] 时有效。",
    )
    time_field: str | None = Field(
        default=None,
        description="事件时间字段名，原样传给 OpenViking；不确定时省略，由服务端默认。",
    )


class MemoryReadInput(BaseModel):
    uri: str = Field(description="记忆条目的 URI，来自 memory_search 或 memory_overview 的结果。")
    max_chars: int = Field(default=8_000, description="最多返回多少字符，1–16000。")


class MemoryOverviewInput(BaseModel):
    uri: str = Field(description="记忆条目的 URI，用来取 L1 摘要。")


class MemoryRememberInput(BaseModel):
    text: str = Field(
        description=(
            "本轮值得长期记住的内容：用户明确要求记住的信息、表达的偏好、"
            "描述的客观事实或情景事件。尽量保留用户原话中的事实、偏好、决定和"
            "事件细节，不要加入推测、总结或额外解释。"
        )
    )


def make_memory_search_tool(facade: Any) -> BaseTool:
    @tool("memory_search", args_schema=MemorySearchInput)
    async def memory_search(
        query: str,
        runtime: ToolRuntime[Any, Any] = None,
        memory_types: list[str] | None = None,
        limit: int = 8,
        since: str | None = None,
        until: str | None = None,
        time_field: str | None = None,
    ) -> str:
        """检索当前用户的长期记忆。memory_types 按问题选择，一次只查需要的类型。"""

        user_id = resolve_runtime_user_id(_context(runtime))
        if not user_id:
            return _error("missing_user", "缺少用户身份，长期记忆不可用")
        types = list(memory_types or DEFAULT_SEARCH_TYPES)
        invalid = sorted(set(types) - set(ALLOWED_MEMORY_TYPES))
        if invalid:
            return _error("invalid_memory_type", f"invalid memory_type: {invalid[0]}")
        if (since or until or time_field) and types != ["events"]:
            return _error(
                "time_filter_requires_events",
                "since/until/time_field require memory_types == ['events']",
            )
        try:
            hits = await facade.search(
                user_id,
                query,
                memory_types=types,
                limit=max(1, min(int(limit), MAX_LIMIT)),
                since=since,
                until=until,
                time_field=time_field,
            )
        except ValueError as exc:
            message = str(exc)
            code = "invalid_memory_type" if "memory_type" in message else "memory_search_failed"
            return _error(code, message)
        except Exception as exc:
            return _error("memory_search_failed", str(exc))
        return json.dumps(
            {"query": query, "items": [_hit(item) for item in hits]},
            ensure_ascii=False,
        )

    return memory_search


def make_memory_read_tool(facade: Any) -> BaseTool:
    @tool("memory_read", args_schema=MemoryReadInput)
    async def memory_read(
        uri: str,
        runtime: ToolRuntime[Any, Any] = None,
        max_chars: int = 8_000,
    ) -> str:
        """读取当前用户的某条 OpenViking 记忆正文。"""

        user_id = resolve_runtime_user_id(_context(runtime))
        if not user_id:
            return _error("missing_user", "缺少用户身份，长期记忆不可用")
        if not 1 <= int(max_chars) <= MAX_READ_CHARS:
            return _error("invalid_max_chars", f"max_chars must be 1..{MAX_READ_CHARS}")
        try:
            return await facade.read(user_id, uri, max_chars=int(max_chars))
        except Exception as exc:
            return _error("memory_read_failed", str(exc))

    return memory_read


def make_memory_overview_tool(facade: Any) -> BaseTool:
    @tool("memory_overview", args_schema=MemoryOverviewInput)
    async def memory_overview(
        uri: str,
        runtime: ToolRuntime[Any, Any] = None,
    ) -> str:
        """读取当前用户某条记忆的 L1 摘要。"""

        user_id = resolve_runtime_user_id(_context(runtime))
        if not user_id:
            return _error("missing_user", "缺少用户身份，长期记忆不可用")
        try:
            return await facade.overview(user_id, uri)
        except Exception as exc:
            return _error("memory_overview_failed", str(exc))

    return memory_overview


def make_memory_remember_tool() -> BaseTool:
    @tool(EXPLICIT_REMEMBER_TOOL_NAME, args_schema=MemoryRememberInput)
    async def memory_remember(
        text: str,
        runtime: ToolRuntime[Any, Any] = None,
    ) -> str:
        """记录本轮值得长期记住的用户信息。

        出现以下情况时调用，无需用户明确要求：
        - 用户明确要求记住，例如「记住……」「记一下……」「以后都……」「别忘了……」。
        - 用户表达稳定的偏好、习惯、好恶或风格倾向，例如「我喜欢……」「我习惯……」。
        - 用户描述客观事实，例如自己的身份、角色、技术栈、项目背景、环境或配置。
        - 用户讲述情景事件，例如发生过的经历、决定、承诺、计划和结果，包含相关的
          时间、地点或参与者。

        不要记录寒暄、一次性提问、临时指令、你自己的推测，或可随时重新获取的信息。
        调用后本轮对话会在回复写完后由记忆流水线提交抽取；返回 queued 只表示已排队，
        不代表记忆已经写入。
        """

        if not isinstance(text, str) or not text.strip():
            return _error("invalid_memory_text", "text must be a non-empty string")
        context = _mutable_context(runtime)
        if context is None:
            return _error("memory_not_active", "当前运行没有可用的记忆上下文")
        set_explicit_remember_signal(context)
        return json.dumps(
            {"ok": True, "status": "queued", "signal": "EXPLICIT_REMEMBER"},
            ensure_ascii=False,
        )

    return memory_remember


def set_explicit_remember_signal(context: MutableMapping[str, Any]) -> TurnDecision:
    """Mark the current turn as EXPLICIT_REMEMBER without writing memory.

    The decision object is replaced rather than mutated because TurnDecision is
    frozen. A separate marker is also set for the (defensive) case where the
    capture middleware has not stored a decision yet; the middleware consumes
    either representation before publishing turn_end.
    """

    context["memory_explicit_remember"] = True
    decision = context.get("capture_decision")
    if not isinstance(decision, TurnDecision):
        return TurnDecision(
            "capture",
            signals=("EXPLICIT_REMEMBER",),
            reason="explicit_remember_tool",
        )
    if "EXPLICIT_REMEMBER" in decision.signals:
        return decision
    updated = replace(
        decision,
        signals=("EXPLICIT_REMEMBER", *decision.signals),
        reason=decision.reason or "explicit_remember_tool",
    )
    context["capture_decision"] = updated
    return updated


def make_explicit_memory_tools() -> list[BaseTool]:
    """Lead-only tools that mark a turn for explicit extraction."""

    return [make_memory_remember_tool()]


def make_openviking_memory_tools(facade: Any) -> list[BaseTool]:
    return [
        make_memory_search_tool(facade),
        make_memory_read_tool(facade),
        make_memory_overview_tool(facade),
    ]


def _context(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    context = getattr(runtime, "context", None)
    return dict(context) if isinstance(context, Mapping) else {}


def _mutable_context(runtime: Any) -> MutableMapping[str, Any] | None:
    context = getattr(runtime, "context", None) if runtime is not None else None
    return context if isinstance(context, MutableMapping) else None


def _hit(hit: MemoryHit) -> dict[str, Any]:
    item = asdict(hit)
    item["tags"] = list(hit.tags)
    return item


def _error(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message}, ensure_ascii=False)


__all__ = [
    "EXPLICIT_REMEMBER_TOOL_NAME",
    "TOOL_NAME",
    "make_explicit_memory_tools",
    "make_memory_overview_tool",
    "make_memory_read_tool",
    "make_memory_remember_tool",
    "make_memory_search_tool",
    "make_openviking_memory_tools",
    "set_explicit_remember_signal",
]
