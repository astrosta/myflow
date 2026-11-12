"""Collapse per-request SystemMessages into the one leading system prompt.

Strict chat templates (vLLM / Qwen / some OpenAI-compatible gateways) reject a
system message unless it is the first message of the conversation. The middleware
chain deliberately inserts more system messages (the dynamic date reminder, the
durable-context authority contract, a subagent role), so the innermost model call
has to normalize the request. Only the ``ModelRequest`` changes; state and the
checkpoint keep the original rows.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from agentkit.agents.middlewares.dynamic_context_middleware import (
    is_dynamic_context_reminder,
)

_SEPARATOR = "\n\n"


def _text(message: SystemMessage) -> str:
    """Injected system content is text. Multimodal blocks keep their text parts."""

    content = message.content
    if isinstance(content, str):
        return content
    return message.text


def _coalesce(request: ModelRequest) -> ModelRequest:
    injected = [message for message in request.messages if isinstance(message, SystemMessage)]
    if not injected:
        # The prompt already lives where strict templates expect it.
        return request

    parts: list[SystemMessage] = []
    if request.system_message is not None:
        parts.append(request.system_message)
    parts.extend(injected)

    # Across midnight the state keeps every reminder; the model only needs the
    # newest one. Non-dynamic system messages are all part of the contract.
    latest_dynamic = max(
        (index for index, message in enumerate(parts) if is_dynamic_context_reminder(message)),
        default=None,
    )
    merged_parts = [
        message
        for index, message in enumerate(parts)
        if not is_dynamic_context_reminder(message) or index == latest_dynamic
    ]
    content = _SEPARATOR.join(
        text for message in merged_parts if (text := _text(message).strip())
    )
    base = request.system_message
    system_message = SystemMessage(
        content=content,
        additional_kwargs=dict(base.additional_kwargs) if base is not None else {},
    )
    return request.override(
        system_message=system_message,
        messages=[
            message
            for message in request.messages
            if not isinstance(message, SystemMessage)
        ],
    )


class SystemMessageCoalescingMiddleware(AgentMiddleware):
    """Give every model call exactly one leading system message."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(_coalesce(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(_coalesce(request))
