"""Turn tool exceptions into error ToolMessages and stamp every successful result."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from agentkit.agents.middlewares.tool_result_meta import normalize_tool_result, stamp_exception_meta
from agentkit.config import AppConfig

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_UNKNOWN_TOOL_NAME = "unknown_tool"
_RECOVERY_HINT = "请基于现有上下文继续，或者换一个工具。"
_DETAIL_LIMIT = 500
# Same key names the gateway drops from persisted config. Harness cannot import that helper.
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(secrets|trace_id|__\w+)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")


def _redact_detail(detail: str) -> str:
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", detail)


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """无构造参数以外只接收 AppConfig。M6 用它盖章；技能读取元数据留给 M7。"""

    def __init__(self, *, app_config: AppConfig | None = None) -> None:
        super().__init__()
        self._app_config = app_config

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_call = request.tool_call or {}
        tool_name = str(tool_call.get("name") or _UNKNOWN_TOOL_NAME)
        tool_call_id = str(tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = _redact_detail(str(exc).strip() or exc.__class__.__name__)
        if len(detail) > _DETAIL_LIMIT:
            detail = detail[: _DETAIL_LIMIT - 3] + "..."
        content = f"错误：工具 '{tool_name}' 调用失败（{exc.__class__.__name__}）：{detail}。{_RECOVERY_HINT}"
        message = ToolMessage(content=content, tool_call_id=tool_call_id, name=tool_name, status="error")
        return stamp_exception_meta(message, f"{exc.__class__.__name__}: {detail}")

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            result = handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception(
                "Tool execution failed (sync): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
        return normalize_tool_result(result, tool_call_id=str(request.tool_call.get("id") or ""))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            result = await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception(
                "Tool execution failed (async): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
        return normalize_tool_result(result, tool_call_id=str(request.tool_call.get("id") or ""))
