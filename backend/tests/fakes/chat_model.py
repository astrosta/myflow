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

    def _result(self) -> ChatResult:
        self.calls += 1
        message = AIMessage(
            content=self.reply,
            id=self.message_id,
            usage_metadata={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
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
