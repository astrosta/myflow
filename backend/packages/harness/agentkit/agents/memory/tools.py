"""The only model-facing memory tool: read-only memory_search (M10 section 2.8.2)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from agentkit.agents.memory.canonical.service import get_memory_service
from agentkit.agents.memory.manager import MemoryManager, get_memory_manager
from agentkit.agents.memory.types import (
    MemoryKind,
    MemoryKindNotSupported,
    MemoryQuery,
    MemoryScope,
    ensure_utc,
)
from agentkit.runtime.user_context import resolve_runtime_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "memory_search"
DEFAULT_KINDS = ["semantic"]
MAX_LIMIT = 20


def make_memory_search_tool(manager: MemoryManager | None = None) -> BaseTool:
    bound_manager = manager

    @tool(TOOL_NAME)
    async def memory_search(
        query: str,
        runtime: ToolRuntime[Any, Any] = None,
        kinds: list[str] | None = None,
        time_range: tuple[str, str] | None = None,
        limit: int = 8,
    ) -> str:
        """检索用户画像中尚未包含的长期记忆。

        用户画像会单独注入。这里只用于检索上方尚未出现的长尾事实；
        默认返回活跃的语义事实。

        Args:
            query: 用自然语言描述要回忆的事实。
            kinds: 可选，要检索的记录类型；默认检索语义事实。
            time_range: 可选，ISO-8601 的 [开始, 结束] 有效时间范围。
            limit: 最多返回多少条事实（1-20）。
        """
        context = _runtime_context(runtime)
        user_id = resolve_runtime_user_id(context)
        if not user_id:
            return json.dumps({"error": "缺少用户身份，长期记忆不可用"}, ensure_ascii=False)
        selected = [kind for kind in (kinds or DEFAULT_KINDS) if kind != MemoryKind.PROFILE]
        if not selected:
            return json.dumps({"error": "用户画像已直接注入，请检索其他类型"}, ensure_ascii=False)
        parsed_range = _parse_time_range(time_range)
        scope = MemoryScope(
            user_id=user_id,
            agent_name=_text(context.get("agent_name")) or _text(context.get("subagent_name")),
            thread_id=_text(context.get("thread_id")),
        )
        manager_instance = bound_manager or get_memory_service() or get_memory_manager()
        if getattr(manager_instance, "canonical", False):
            try:
                page = await manager_instance.search(
                    user_id,
                    text=query,
                    kinds=selected,
                    limit=max(1, min(int(limit), MAX_LIMIT)),
                )
                items = [
                    {
                        "kind": hit.record.kind,
                        "subtype": hit.record.subtype,
                        "id": hit.record.id,
                        "content": hit.record.content,
                        "topic_key": hit.record.topic_key,
                        "confidence": hit.record.confidence,
                        "updated_at": hit.record.updated_at.isoformat(),
                    }
                    for hit in page.items
                ]
                return json.dumps(
                    {"query": query, "kinds": selected, "items": items, "next_cursor": page.next_cursor},
                    ensure_ascii=False,
                )
            except Exception:
                logger.warning("memory_search failed", exc_info=True)
                return json.dumps({"error": "长期记忆检索失败"}, ensure_ascii=False)
        try:
            page = await asyncio.to_thread(
                manager_instance.search,
                scope,
                MemoryQuery(
                    kinds=selected,
                    text=query,
                    time_range=parsed_range,
                    limit=max(1, min(int(limit), MAX_LIMIT)),
                    sort="relevance",
                ),
            )
            semantic = manager_instance.supported_kinds and MemoryKind.SEMANTIC in selected
            await asyncio.to_thread(manager_instance.note_search, scope)
            items = []
            for hit in page.items:
                record = hit.record
                conflict = record.status == "conflicted"
                items.append(
                    {
                        "kind": record.kind,
                        "subtype": record.subtype,
                        "id": record.id,
                        "content": record.content,
                        "topic_key": record.topic_key,
                        "confidence": record.confidence,
                        "updated_at": ensure_utc(record.updated_at).isoformat(),
                        **({"conflict": True} if conflict else {}),
                    }
                )
                if semantic and record.kind == MemoryKind.SEMANTIC:
                    try:
                        await asyncio.to_thread(
                            manager_instance.service(MemoryKind.SEMANTIC).touch,  # type: ignore[attr-defined]
                            scope,
                            record.id,
                        )
                    except Exception:
                        logger.debug("Failed to record memory access", exc_info=True)
            return json.dumps(
                {"query": query, "kinds": selected, "items": items, "next_cursor": page.next_cursor},
                ensure_ascii=False,
            )
        except MemoryKindNotSupported as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except Exception:
            logger.warning("memory_search failed", exc_info=True)
            return json.dumps({"error": "长期记忆检索失败"}, ensure_ascii=False)


    return memory_search


def _runtime_context(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    context = getattr(runtime, "context", None)
    return dict(context) if isinstance(context, Mapping) else {}


def _parse_time_range(value: tuple[str, str] | None) -> tuple[datetime, datetime] | None:
    if not value:
        return None
    try:
        start, end = value
        return ensure_utc(datetime.fromisoformat(start)), ensure_utc(datetime.fromisoformat(end))
    except (TypeError, ValueError):
        raise ValueError("time_range must contain two ISO-8601 timestamps")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
