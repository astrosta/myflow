"""Read-only model tools backed by the OpenViking recall service."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.runtime.user_context import resolve_runtime_user_id

DEFAULT_MEMORY_TYPES = ["preferences", "entities", "events"]
MAX_LIMIT = 20
MAX_READ_CHARS = 16_000


def make_memory_search_tool(facade: Any) -> BaseTool:
    @tool("memory_search")
    async def memory_search(
        query: str,
        runtime: ToolRuntime[Any, Any] = None,
        memory_types: list[str] | None = None,
        limit: int = 8,
        since: str | None = None,
        until: str | None = None,
        time_field: str | None = None,
    ) -> str:
        """检索用户长期记忆；画像由系统单独注入。"""

        user_id = resolve_runtime_user_id(_context(runtime))
        if not user_id:
            return _error("missing_user", "缺少用户身份，长期记忆不可用")
        try:
            hits = await facade.search(
                user_id,
                query,
                memory_types=memory_types or DEFAULT_MEMORY_TYPES,
                limit=max(1, min(int(limit), MAX_LIMIT)),
                since=since,
                until=until,
                time_field=time_field,
            )
        except Exception as exc:
            return _error("memory_search_failed", str(exc))
        return json.dumps(
            {"query": query, "items": [_hit(item) for item in hits]},
            ensure_ascii=False,
        )

    return memory_search


def make_memory_read_tool(facade: Any) -> BaseTool:
    @tool("memory_read")
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
    @tool("memory_overview")
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
    return dict(context) if isinstance(context, dict) else {}


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
