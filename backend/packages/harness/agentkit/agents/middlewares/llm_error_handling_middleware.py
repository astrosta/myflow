"""Turn provider failures into a visible AIMessage, retrying transient errors.

Circuit breaking and the process-wide concurrency cap stay out of this wave.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

from agentkit.config import AppConfig
from agentkit.utils.custom_events import aemit_custom_event, emit_custom_event

logger = logging.getLogger(__name__)

_RETRIABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_TRANSIENT_CLASS_NAMES = frozenset({
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",
    "ReadError",
    "RemoteProtocolError",
})
_QUOTA_PATTERNS = ("quota", "billing", "credit", "payment", "余额不足", "超出限额")
_AUTH_PATTERNS = ("authentication", "unauthorized", "invalid api key", "forbidden", "未授权")
_BUSY_PATTERNS = ("server busy", "overloaded", "rate limit", "服务繁忙")
_DETAIL_LIMIT = 500
# Same key names the gateway drops from persisted config. Harness cannot import that helper.
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(secrets|trace_id|__\w+)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")
_QUOTA_MESSAGE = (
    "所配置的大模型服务商拒绝了本次请求，可能是账户额度不足、计费不可用或使用受限。"
    "请修复服务商账户状态后重试。"
)
_AUTH_MESSAGE = (
    "所配置的大模型服务商拒绝了本次请求，认证信息或访问权限无效。"
    "请检查服务商凭据后重试。"
)
_UNAVAILABLE_MESSAGE = (
    "所配置的大模型服务商在多次重试后仍暂时不可用。"
    "请稍候片刻再继续对话。"
)


def _matches_any(detail: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in detail for pattern in patterns)


def _extract_error_code(exc: BaseException) -> object:
    for attr in ("code", "error_code"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            return value
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("code", "type"):
                value = error.get(key)
                if value not in (None, ""):
                    return value
    return None


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _extract_retry_after_ms(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        return None
    raw = None
    for key in ("Retry-After", "retry-after"):
        raw = headers.get(key)
        if raw:
            break
    if not raw:
        return None
    try:
        return max(0, int(float(raw) * 1000))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            delta = target.timestamp() - time.time()
            return max(0, int(delta * 1000))
        except (TypeError, ValueError, OverflowError, OSError):
            return None


def _sanitize_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if not detail:
        message = getattr(exc, "message", None)
        detail = message.strip() if isinstance(message, str) and message.strip() else type(exc).__name__
    detail = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", detail)
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[: _DETAIL_LIMIT - 3] + "..."
    return detail


class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """必填 AppConfig。只读 llm_call 的重试参数，构造时拍进 self。"""

    def __init__(self, *, app_config: AppConfig) -> None:
        super().__init__()
        llm_call = app_config.llm_call
        self.retry_max_attempts = llm_call.retry_max_attempts
        self.retry_base_delay_ms = llm_call.retry_base_delay_ms
        self.retry_cap_delay_ms = llm_call.retry_cap_delay_ms

    def _classify_error(self, exc: BaseException) -> tuple[bool, str]:
        detail = str(exc).lower()
        error_code = _extract_error_code(exc)
        if _matches_any(detail, _QUOTA_PATTERNS) or (
            error_code is not None and _matches_any(str(error_code).lower(), _QUOTA_PATTERNS)
        ):
            return False, "quota"
        if _matches_any(detail, _AUTH_PATTERNS):
            return False, "auth"
        if type(exc).__name__ in _TRANSIENT_CLASS_NAMES or isinstance(exc, IndexError):
            return True, "transient"
        if _extract_status_code(exc) in _RETRIABLE_STATUS_CODES:
            return True, "transient"
        if _matches_any(detail, _BUSY_PATTERNS):
            return True, "busy"
        return False, "generic"

    def _retry_delay_ms(self, prev_delay_ms: int | None, exc: BaseException) -> int:
        retry_after = _extract_retry_after_ms(exc)
        if retry_after is not None:
            return retry_after
        base = self.retry_base_delay_ms
        cap = self.retry_cap_delay_ms
        seed = base if prev_delay_ms is None else prev_delay_ms
        high = min(cap, max(base, seed * 3))
        if high < base:
            return cap
        return random.randint(base, high)

    def _retry_message(self, attempt: int, wait_ms: int, reason: str) -> str:
        seconds = max(1, round(wait_ms / 1000))
        reason_text = "provider is busy" if reason == "busy" else "provider request failed temporarily"
        return (
            f"LLM request retry {attempt}/{self.retry_max_attempts}: {reason_text}. Retrying in {seconds}s."
        )

    def _retry_event(self, attempt: int, wait_ms: int, reason: str) -> dict:
        return {
            "type": "llm_retry",
            "attempt": attempt,
            "max_attempts": self.retry_max_attempts,
            "wait_ms": wait_ms,
            "reason": reason,
            "message": self._retry_message(attempt, wait_ms, reason),
        }

    def _emit_retry_event(self, attempt: int, wait_ms: int, reason: str) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            emit_custom_event(self._retry_event(attempt, wait_ms, reason), writer=writer)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.debug("Failed to emit llm_retry event", exc_info=True)

    async def _aemit_retry_event(self, attempt: int, wait_ms: int, reason: str) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            await aemit_custom_event(self._retry_event(attempt, wait_ms, reason), writer=writer)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.debug("Failed to emit llm_retry event", exc_info=True)

    def _user_message(self, reason: str, detail: str) -> str:
        if reason == "quota":
            return _QUOTA_MESSAGE
        if reason == "auth":
            return _AUTH_MESSAGE
        if reason in {"busy", "transient"}:
            return _UNAVAILABLE_MESSAGE
        return f"LLM 请求失败：{detail}"

    def _fallback_ai_message(self, exc: BaseException, reason: str) -> AIMessage:
        detail = _sanitize_detail(exc)
        return AIMessage(
            content=self._user_message(reason, detail),
            additional_kwargs={
                "myflow_error_fallback": True,
                "error_type": type(exc).__name__,
                "error_reason": reason,
                "error_detail": detail,
            },
        )

    def _run(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        attempt = 1
        prev_delay_ms: int | None = None
        while True:
            try:
                return handler(request)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                if retriable and attempt < self.retry_max_attempts:
                    wait_ms = self._retry_delay_ms(prev_delay_ms, exc)
                    prev_delay_ms = wait_ms
                    self._emit_retry_event(attempt, wait_ms, reason)
                    time.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning("LLM call failed after %d attempt(s): %s", attempt, _sanitize_detail(exc))
                return self._fallback_ai_message(exc, reason)

    async def _arun(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        attempt = 1
        prev_delay_ms: int | None = None
        while True:
            try:
                return await handler(request)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                if retriable and attempt < self.retry_max_attempts:
                    wait_ms = self._retry_delay_ms(prev_delay_ms, exc)
                    prev_delay_ms = wait_ms
                    await self._aemit_retry_event(attempt, wait_ms, reason)
                    await asyncio.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning("LLM call failed after %d attempt(s): %s", attempt, _sanitize_detail(exc))
                return self._fallback_ai_message(exc, reason)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return self._run(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await self._arun(request, handler)
