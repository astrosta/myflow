"""Read-only model tools backed by the OpenViking recall service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from agentkit.agents.memory.openviking.config import DEFAULT_MEMORY_TYPES as ALLOWED_MEMORY_TYPES
from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.runtime.user_context import resolve_runtime_user_id

DEFAULT_SEARCH_TYPES = ["preferences", "entities", "events"]
MAX_LIMIT = 20
MAX_READ_CHARS = 16_000

MemoryTypeArg = Literal[
    "preferences",
    "entities",
    "events",
    "cases",
    "trajectories",
    "experiences",
    "tools",
    "skills",
]


class MemorySearchInput(BaseModel):
    query: str = Field(description="要回忆的事实，写成自包含的一句话。")
    memory_types: list[MemoryTypeArg] | None = Field(
        default=None,
        description=(
            "检索范围。合法值：preferences、entities、events、cases、trajectories、"
            "experiences、tools、skills。默认 preferences/entities/events（不含经验与案例）；"
            "查经验用 experiences，查案例用 cases。"
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
        """检索当前用户的长期记忆。"""

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


def _hit(hit: MemoryHit) -> dict[str, Any]:
    item = asdict(hit)
    item["tags"] = list(hit.tags)
    return item


def _error(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message}, ensure_ascii=False)


__all__ = [
    "make_memory_overview_tool",
    "make_memory_read_tool",
    "make_memory_search_tool",
    "make_openviking_memory_tools",
]
