"""Deterministic chat model for callback tests (M4 §11).

It has to be a real `BaseChatModel`: a hand-rolled object that just returns a
string triggers none of the callbacks the ledger depends on (M4 §5.7 第 3 条).
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeChatModel(BaseChatModel):
    """每次调用回一条固定的 AI 消息，并带上 usage。"""

    reply: str = "fake reply"
    input_tokens: int = 3
    output_tokens: int = 5
    message_id: str | None = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeChatModel":
        return self  # create_agent 一定会调它

    def _usage(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }

    def _result(self) -> ChatResult:
        self.calls += 1
        message = AIMessage(
            content=self.reply,
            id=self.message_id,
            usage_metadata=self._usage(),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._result()

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._result()


class ScriptedChatModel(FakeChatModel):
    """按脚本回消息：每次调用取一条，用完就一直重复最后一条。

    ``replies`` 里放字符串或整条 ``AIMessage``（后者可以带 ``tool_calls``）。
    父图要先发一个 ``task`` 调用、再给最终答复时用它（M8 §7.3 的血缘回归）。
    """

    replies: list[Any] = []

    def _result(self) -> ChatResult:
        index = min(self.calls, len(self.replies) - 1) if self.replies else -1
        self.calls += 1
        item = self.replies[index] if index >= 0 else self.reply
        message = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        message = message.model_copy(update={"usage_metadata": self._usage()})
        return ChatResult(generations=[ChatGeneration(message=message)])
