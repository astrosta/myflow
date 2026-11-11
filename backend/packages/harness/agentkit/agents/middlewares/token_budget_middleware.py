"""子代理 token 预算（M8 §12.5 / §13）。

预算按累计 provider 用量计算，而不是只算当前上下文。到硬上限时：
剥掉本轮 tool_calls、强制 finish_reason=stop，并留下一条明确的“部分结果”说明。
不抛异常——让本轮自然结束，lead 才能拿到已经完成的内容。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware

from agentkit.agents.middlewares.message_utils import clone_ai_message_with_tool_calls
from agentkit.utils.messages import message_content_to_text

logger = logging.getLogger(__name__)

TOKEN_BUDGET_NOTICE = (
    "\n\n[已达到子代理 token 预算（{used:,}/{max_tokens:,}），已停止继续调用工具。"
    "{tail}]"
)
TOKEN_BUDGET_PARTIAL_TAIL = "以下为已取得的部分结果。"
TOKEN_BUDGET_EMPTY_TAIL = "没有形成最终答复，请基于已有步骤判断是否重试。"
#: 盖在“只有工具调用、没有正文”的被截断消息上：执行器据此判定失败，
#: 而不是把更早的一条中间思考误当成最终回答。
TOKEN_BUDGET_NO_RESULT_KEY = "myflow_token_budget_no_result"


class TokenBudgetMiddleware(AgentMiddleware[AgentState]):
    """每个子代理执行一份实例；usage_reader 读的是这次执行的累计快照。

    到上限时 intentionally 不抛异常：执行器会在收尾时通过
    consume_stop_reason 拿到 token_capped，状态仍是 completed/failed。
    """

    def __init__(
        self,
        *,
        max_tokens: int,
        warn_threshold: float,
        usage_reader: Callable[[], int],
        subagent_name: str = "",
    ) -> None:
        super().__init__()
        self._max_tokens = max(1, int(max_tokens))
        self._warn_threshold = min(1.0, max(0.0, float(warn_threshold)))
        self._warn_tokens = max(1, int(self._max_tokens * self._warn_threshold))
        self._usage_reader = usage_reader
        self._subagent_name = subagent_name or "subagent"
        self._warned = False
        self._capped = False

    def after_model(self, state, runtime) -> dict | None:
        del runtime
        return self._enforce(state)

    async def aafter_model(self, state, runtime) -> dict | None:
        del runtime
        return self._enforce(state)

    def consume_stop_reason(self, _run_id: str | None = None) -> str | None:
        """执行器的 duck-typing 入口；每个实例只服务一次执行，不需要 run_id 分桶。"""
        return "token_capped" if self._capped else None

    def _enforce(self, state) -> dict | None:
        used = self._used_tokens()
        if not self._warned and used >= self._warn_tokens:
            self._warned = True
            logger.warning(
                "Subagent %s token usage crossed the warning threshold (%d/%d)",
                self._subagent_name,
                used,
                self._max_tokens,
            )
        if used < self._max_tokens:
            return None

        self._capped = True
        logger.warning(
            "Subagent %s exceeded its token budget (%d/%d); stopping tool calls",
            self._subagent_name,
            used,
            self._max_tokens,
        )
        message = _last_ai_message(state)
        if message is None:
            return None

        text = message_content_to_text(getattr(message, "content", "") or "")
        notice = TOKEN_BUDGET_NOTICE.format(
            used=used,
            max_tokens=self._max_tokens,
            tail=TOKEN_BUDGET_PARTIAL_TAIL if text.strip() else TOKEN_BUDGET_EMPTY_TAIL,
        )
        next_message = _append_notice(message, notice)
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if tool_calls:
            next_message = clone_ai_message_with_tool_calls(next_message, [])
            next_message = next_message.model_copy(update={"invalid_tool_calls": []})
        if not text.strip():
            next_message = next_message.model_copy(
                update={
                    "additional_kwargs": {
                        **(getattr(next_message, "additional_kwargs", None) or {}),
                        TOKEN_BUDGET_NO_RESULT_KEY: True,
                    }
                }
            )
        return {"messages": [_force_stop(next_message)]}

    def _used_tokens(self) -> int:
        try:
            return max(0, int(self._usage_reader() or 0))
        except Exception:  # noqa: BLE001 - a broken counter must not break the agent
            logger.exception("Subagent %s token usage reader failed", self._subagent_name)
            return 0


def _last_ai_message(state) -> object | None:
    for message in reversed(list(state.get("messages") or [])):
        if getattr(message, "type", None) == "ai":
            return message
    return None


def _append_notice(message, notice: str) -> object:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        new_content: object = f"{content}\n\n{notice}" if content.strip() else notice
    elif isinstance(content, list):
        new_content = [*content, {"type": "text", "text": notice}]
    else:
        new_content = notice
    return message.model_copy(update={"content": new_content})


def _force_stop(message) -> object:
    return message.model_copy(
        update={
            "response_metadata": {
                **(getattr(message, "response_metadata", None) or {}),
                "finish_reason": "stop",
            }
        }
    )


__all__ = ["TokenBudgetMiddleware"]
