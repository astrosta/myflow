"""Resolve the one chat model used by LangMem A/B/C/D.

Memory operators deliberately do not follow a per-run model.  They always use
the agent default, or the first configured model when no default is set.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from agentkit.config.memory_config import MemoryConfigError
from agentkit.models import create_chat_model


def operator_model_name(app_config: Any) -> str:
    """Return the configured operator model name or fail with a clear error."""
    agents = getattr(app_config, "agents", None)
    default = getattr(agents, "default_model", None)
    if isinstance(default, str) and default.strip():
        return default

    models = getattr(app_config, "models", None) or []
    if models:
        first = getattr(models[0], "name", None)
        if isinstance(first, str) and first.strip():
            return first

    raise MemoryConfigError(
        "memory operator model is not configured: set agents.default_model or models[0]"
    )


def resolve_operator_model(app_config: Any) -> BaseChatModel:
    """Build the stateless operator model with thinking/tracing disabled."""
    return create_chat_model(
        name=operator_model_name(app_config),
        thinking_enabled=False,
        attach_tracing=False,
        app_config=app_config,
    )
