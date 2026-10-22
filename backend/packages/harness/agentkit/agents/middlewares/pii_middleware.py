"""Run-scoped PII redaction at the agent boundary.

Input and output are projected through a PIIVault that only lives in runtime.context.
The vault is not serialized, is not put in state, and expires with the run.

Scope: genuine HumanMessage text, model text/tool-call output, and every tool's
arguments at the trusted call boundary. ToolMessage results are owned by the tool and
are not redacted by this module.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime

from agentkit.config import AppConfig, get_app_config
from agentkit.config.pii_config import PIIConfig
from agentkit.privacy import (
    PIIRedactor,
    PIIUnresolvedToken,
    PIIVault,
    redact_ai_messages,
    redact_human_messages,
)
from agentkit.runtime.secret_context import PII_VAULT_CONTEXT_KEY

logger = logging.getLogger(__name__)


def _runtime_context(runtime: Any) -> Mapping[str, Any] | None:
    context = getattr(runtime, "context", None) if runtime is not None else None
    return context if isinstance(context, Mapping) else None


def get_or_create_vault(runtime: Any) -> PIIVault:
    context = _runtime_context(runtime)
    if isinstance(context, MutableMapping):
        vault = context.get(PII_VAULT_CONTEXT_KEY)
        if not isinstance(vault, PIIVault):
            vault = PIIVault()
            context[PII_VAULT_CONTEXT_KEY] = vault
        return vault
    # A read-only context is unusual; keep a middleware-local vault rather than
    # silently falling back to raw text. Tool restore will fail closed.
    return PIIVault()


class PIIMiddleware(AgentMiddleware):
    """Redact genuine user messages and restore handles for every tool call."""

    def __init__(self, *, app_config: AppConfig | None = None) -> None:
        super().__init__()
        self._app_config = app_config

    def _config(self) -> PIIConfig:
        if self._app_config is not None:
            return self._app_config.pii
        try:
            return get_app_config().pii
        except Exception:
            return PIIConfig()

    def before_agent(self, state: Mapping, runtime: Runtime) -> dict | None:
        config = self._config()
        if not config.enabled:
            return None
        messages = list(state.get("messages") or [])
        vault = get_or_create_vault(runtime)
        redacted = redact_human_messages(messages, PIIRedactor(vault))
        if redacted == messages:
            return None
        return {"messages": redacted}

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        return self._wrap_tool_call(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        return await self._awrap_tool_call(request, handler)

    def _wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        try:
            return handler(self._restore_request(request))
        except PIIUnresolvedToken:
            raise

    async def _awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        try:
            return await handler(self._restore_request(request))
        except PIIUnresolvedToken:
            raise

    def _restore_request(self, request: ToolCallRequest) -> ToolCallRequest:
        config = self._config()
        if not config.enabled:
            return request
        tool_call = request.tool_call or {}
        vault = get_or_create_vault(getattr(request, "runtime", None))
        redactor = PIIRedactor(vault)
        args = redactor.restore_payload(tool_call.get("args"))
        if args == tool_call.get("args"):
            return request
        return request.override(tool_call={**tool_call, "args": args})


class PIIOutputMiddleware(AgentMiddleware):
    """Redact model output before it reaches state, checkpoints, or memory."""

    def __init__(self, *, app_config: AppConfig | None = None) -> None:
        super().__init__()
        self._app_config = app_config

    def _config(self) -> PIIConfig:
        if self._app_config is not None:
            return self._app_config.pii
        try:
            return get_app_config().pii
        except Exception:
            return PIIConfig()

    def after_model(self, state: Mapping, runtime: Runtime) -> dict | None:
        config = self._config()
        if not config.enabled or not config.redact_ai_output:
            return None
        messages = list(state.get("messages") or [])
        vault = get_or_create_vault(runtime)
        redacted = redact_ai_messages(messages, PIIRedactor(vault))
        if redacted == messages:
            return None
        return {"messages": redacted}

    async def aafter_model(self, state: Mapping, runtime: Runtime) -> dict | None:
        return self.after_model(state, runtime)


__all__ = [
    "PII_VAULT_CONTEXT_KEY",
    "PIIMiddleware",
    "PIIOutputMiddleware",
    "get_or_create_vault",
]
