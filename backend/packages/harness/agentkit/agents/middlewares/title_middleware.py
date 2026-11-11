"""Write a thread title after the first complete exchange."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.constants import TAG_NOSTREAM
from langgraph.runtime import Runtime

from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.config import AppConfig
from agentkit.config.title_config import TitleConfig, get_title_config
from agentkit.models import create_chat_model
from agentkit.utils.messages import get_original_user_content_text, message_content_to_text

logger = logging.getLogger(__name__)

_DEFAULT_TITLE = "新对话"
_PROMPT_SIDE_LIMIT = 500
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


class TitleMiddlewareState(AgentState):
    title: NotRequired[str | None]


def _message_type(message: object) -> str | None:
    if isinstance(message, dict):
        message_type = message.get("type") or message.get("role")
        if message_type == "user":
            return "human"
        if message_type == "assistant":
            return "ai"
    else:
        message_type = getattr(message, "type", None)
    return message_type if isinstance(message_type, str) else None


def _message_content(message: object) -> object:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _additional_kwargs(message: object) -> Mapping | None:
    if isinstance(message, dict):
        kwargs = message.get("additional_kwargs")
    else:
        kwargs = getattr(message, "additional_kwargs", None)
    return kwargs if isinstance(kwargs, Mapping) else None


def _is_user_message(message: object) -> bool:
    return is_real_user_message(message)


def _strip_think_tags(text: str) -> str:
    return _THINK_BLOCK.sub("", text).strip()


class TitleMiddleware(AgentMiddleware[TitleMiddlewareState]):
    """Prefer an explicit TitleConfig, then app_config.title, then the process snapshot."""

    state_schema = TitleMiddlewareState

    def __init__(self, *, app_config: AppConfig | None = None, title_config: TitleConfig | None = None) -> None:
        super().__init__()
        self._app_config = app_config
        self._title_config = title_config

    def _resolved_config(self) -> TitleConfig:
        if self._title_config is not None:
            return self._title_config
        if self._app_config is not None:
            return self._app_config.title
        return get_title_config()

    def _should_generate_title(self, state: Mapping, *, allow_partial_exchange: bool = False) -> bool:
        config = self._resolved_config()
        if not config.enabled or state.get("title"):
            return False
        messages = state.get("messages") or []
        if len(messages) < (1 if allow_partial_exchange else 2):
            return False
        user_count = sum(1 for message in messages if _is_user_message(message))
        assistant_count = sum(1 for message in messages if _message_type(message) == "ai")
        return user_count == 1 and (assistant_count >= 1 or allow_partial_exchange)

    def _user_text(self, state: Mapping) -> str:
        for message in state.get("messages") or []:
            if _is_user_message(message):
                return get_original_user_content_text(_message_content(message), _additional_kwargs(message))
        return ""

    def _assistant_text(self, state: Mapping) -> str:
        for message in state.get("messages") or []:
            if _message_type(message) == "ai":
                return _strip_think_tags(message_content_to_text(_message_content(message)))
        return ""

    def _fallback_title(self, user_msg: str) -> str:
        config = self._resolved_config()
        fallback_chars = min(config.max_chars, 50)
        if len(user_msg) > fallback_chars:
            ellipsis = "..."
            body = min(fallback_chars, config.max_chars - len(ellipsis))
            return user_msg[:body].rstrip() + ellipsis
        return user_msg

    def _parse_title(self, content: object) -> str:
        title = _strip_think_tags(message_content_to_text(content)).strip().strip('"').strip("'")
        max_chars = self._resolved_config().max_chars
        if len(title) > max_chars:
            return title[:max_chars]
        return title

    def _prompt(self, state: Mapping, user_msg: str) -> str:
        config = self._resolved_config()
        return config.prompt_template.format(
            max_words=config.max_words,
            user_msg=user_msg[:_PROMPT_SIDE_LIMIT],
            assistant_msg=self._assistant_text(state)[:_PROMPT_SIDE_LIMIT],
        )

    def _runnable_config(self) -> dict[str, Any]:
        try:
            from langgraph.config import get_config

            parent = dict(get_config())
        except Exception:
            parent = {}
        parent["run_name"] = "title_agent"
        parent["tags"] = [*(parent.get("tags") or []), "middleware:title", TAG_NOSTREAM]
        return parent

    def _local_title(self, state: Mapping, *, allow_partial_exchange: bool = False) -> dict | None:
        if not self._should_generate_title(state, allow_partial_exchange=allow_partial_exchange):
            return None
        user_msg = self._user_text(state)
        if not user_msg.strip():
            return {"title": _DEFAULT_TITLE}
        return {"title": self._fallback_title(user_msg)}

    def _generate_title_result(self, state: Mapping, *, allow_partial_exchange: bool = False) -> dict | None:
        """Sync path never calls a model."""
        return self._local_title(state, allow_partial_exchange=allow_partial_exchange)

    def _title_model_name(self, config: TitleConfig) -> str | None:
        """Resolve the model used for title extraction.

        An explicit ``model_name`` wins. Otherwise production assemblies use the
        same default model as the conversation; only a standalone middleware
        (tests / CLI without an app snapshot) keeps the local truncation path.
        """

        if not config.use_model:
            return None
        if config.model_name:
            return config.model_name
        if self._app_config is not None:
            if self._app_config.agents.default_model:
                return self._app_config.agents.default_model
            # Match the chat factory's "no explicit default = first model" rule.
            return self._app_config.models[0].name if self._app_config.models else None
        return None

    async def _agenerate_title_result(self, state: Mapping) -> dict | None:
        if not self._should_generate_title(state):
            return None
        user_msg = self._user_text(state)
        if not user_msg.strip():
            return {"title": _DEFAULT_TITLE}
        config = self._resolved_config()
        model_name = self._title_model_name(config)
        if not model_name:
            return {"title": self._fallback_title(user_msg)}
        try:
            model_kwargs: dict[str, Any] = {
                "name": model_name,
                "thinking_enabled": False,
                "attach_tracing": False,
            }
            if self._app_config is not None:
                model_kwargs["app_config"] = self._app_config
            model = create_chat_model(**model_kwargs)
            response = await model.ainvoke(self._prompt(state, user_msg), config=self._runnable_config())
            title = self._parse_title(getattr(response, "content", response))
            if title:
                return {"title": title}
        except Exception:
            logger.warning("Title model call failed; using the local title", exc_info=True)
        return {"title": self._fallback_title(user_msg)}

    def after_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        del runtime
        return self._generate_title_result(state)

    async def aafter_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        del runtime
        return await self._agenerate_title_result(state)


def fallback_title_from_state(state: Mapping, *, title_config: TitleConfig) -> dict | None:
    """Worker finally calls this. It does not construct a model."""
    return TitleMiddleware(title_config=title_config)._generate_title_result(state, allow_partial_exchange=True)
