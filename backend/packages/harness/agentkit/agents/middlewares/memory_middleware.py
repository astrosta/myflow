"""Profile injection and conversation enqueue middleware (M10 section 2.6.1 / 2.8.1)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.middlewares.provenance import ContentKind, provenance_kwargs
from agentkit.agents.memory.canonical.service import (
    MemoryService,
    get_memory_service,
    render_profile_view,
)
from agentkit.agents.memory.context import ProfileContextBuilder
from agentkit.agents.memory.manager import MemoryManager, get_memory_manager
from agentkit.agents.memory.types import MemoryScope
from agentkit.config.memory_config import MemoryConfig
from agentkit.utils.custom_events import emit_custom_event
from agentkit.utils.messages import message_content_to_text

logger = logging.getLogger(__name__)

MEMORY_PROFILE_MESSAGE_ID = "__user_profile__"
MEMORY_PROFILE_KEY = "memory_profile"
MEMORY_PROFILE_REVISION_KEY = "memory_profile_revision"
MEMORY_PROFILE_HASH_KEY = "memory_profile_sha256"
MEMORY_PROFILE_TOKENS_KEY = "memory_profile_tokens"
MEMORY_PROFILE_TRUNCATED_KEY = "memory_profile_truncated"
_PROFILE_READ_TIMEOUT_SECONDS = 5.0


class MemoryMiddleware(AgentMiddleware):
    """Inject the resident profile once per thread, then enqueue visible turns."""

    def __init__(
        self,
        *,
        manager: MemoryManager | None = None,
        service: MemoryService | None = None,
        config: MemoryConfig | None = None,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._service = service
        self._config = config
        self._user_id = user_id
        self._agent_name = agent_name

    def _resolved_config(self) -> MemoryConfig:
        if self._config is not None:
            return self._config
        service = self._resolved_service()
        if service is not None:
            return service.config
        manager = self._manager or get_memory_manager()
        return manager.config

    def _resolved_manager(self) -> MemoryManager:
        return self._manager or get_memory_manager()

    def _resolved_service(self) -> MemoryService | None:
        return self._service or get_memory_service()

    def before_agent(self, state: Mapping, runtime: Runtime) -> dict | None:
        service = self._resolved_service()
        if service is None:
            return self._inject_profile(state, runtime)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._inject_profile_service(state, runtime))
        return None

    async def abefore_agent(self, state: Mapping, runtime: Runtime) -> dict | None:
        service = self._resolved_service()
        if service is not None:
            try:
                return await asyncio.wait_for(
                    self._inject_profile_service(state, runtime),
                    timeout=_PROFILE_READ_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Memory profile injection timed out after %.1fs; continuing without it",
                    _PROFILE_READ_TIMEOUT_SECONDS,
                )
                return None
        config = self._resolved_config()
        if not config.enabled or not config.profile.injection_enabled:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inject_profile, state, runtime),
                timeout=_PROFILE_READ_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Memory profile injection timed out after %.1fs; continuing without it",
                _PROFILE_READ_TIMEOUT_SECONDS,
            )
            return None

    def after_model(self, state: Mapping, runtime: Runtime) -> dict | None:
        if self._resolved_service() is None:
            self._enqueue_turn(state, runtime)
        return None

    async def aafter_model(self, state: Mapping, runtime: Runtime) -> dict | None:
        if self._resolved_service() is None:
            self._enqueue_turn(state, runtime)
        return None

    async def aafter_agent(self, state: Mapping, runtime: Runtime) -> dict | None:
        service = self._resolved_service()
        if service is not None:
            await self._capture_turn_service(service, state, runtime)
        return None

    # -- profile injection --------------------------------------------------

    def _inject_profile(self, state: Mapping, runtime: Runtime) -> dict | None:
        config = self._resolved_config()
        if not config.enabled or not config.profile.injection_enabled:
            return None
        context = _context(runtime)
        if _text(context.get("subagent_name")) is not None:
            return None  # lead only: subagents do not re-inject the whole profile
        user_id = _text(context.get("user_id")) or self._user_id
        if not user_id:
            return None
        messages = list(state.get("messages") or [])
        existing_index = _profile_message_index(messages)
        if config.profile.refresh == "thread_start" and existing_index is not None:
            return None
        try:
            snapshot = self._resolved_manager().service("profile").snapshot(  # type: ignore[attr-defined]
                MemoryScope(
                    user_id=user_id,
                    agent_name=_text(context.get("agent_name")) or self._agent_name,
                    thread_id=_text(context.get("thread_id")),
                )
            )
            if snapshot is None:
                return None
            builder = ProfileContextBuilder(
                max_tokens=config.profile.max_tokens,
                token_counting=str(config.backend_config.get("token_counting", "char")),
            )
            rendered = builder.build(snapshot)
            if not rendered.text:
                return None
        except Exception:
            logger.warning("Memory profile read failed; continuing with the date only", exc_info=True)
            return None
        profile_message = HumanMessage(
            content=rendered.text,
            id=MEMORY_PROFILE_MESSAGE_ID,
            additional_kwargs={
                "hide_from_ui": True,
                MEMORY_PROFILE_KEY: True,
                MEMORY_PROFILE_REVISION_KEY: snapshot.revision,
                MEMORY_PROFILE_HASH_KEY: hashlib.sha256(rendered.text.encode("utf-8")).hexdigest(),
                MEMORY_PROFILE_TOKENS_KEY: rendered.token_count,
                MEMORY_PROFILE_TRUNCATED_KEY: rendered.truncated,
                **provenance_kwargs(ContentKind.MEMORY, "profile_context"),
            },
        )
        if existing_index is not None:
            existing = messages[existing_index]
            if _same_profile_message(existing, profile_message):
                return None
            messages[existing_index] = profile_message
        else:
            target = _latest_real_user_index(messages)
            if target is None:
                return None
            messages.insert(target, profile_message)
        _emit_profile_event(
            snapshot,
            rendered,
            agent_name=_text(context.get("agent_name")) or self._agent_name,
        )
        return {"messages": messages}

    async def _inject_profile_service(self, state: Mapping, runtime: Runtime) -> dict | None:
        service = self._resolved_service()
        if service is None:
            return None
        config = self._resolved_config()
        if not config.enabled or not config.profile.injection_enabled:
            return None
        context = _context(runtime)
        if _text(context.get("subagent_name")) is not None:
            return None
        user_id = _text(context.get("user_id")) or self._user_id
        if not user_id:
            return None
        messages = list(state.get("messages") or [])
        existing_index = _profile_message_index(messages)
        if config.profile.refresh == "thread_start" and existing_index is not None:
            return None
        try:
            snapshot = await service.get_profile(user_id)
            rendered = render_profile_view(snapshot, config.profile.max_tokens)
            if not rendered.text:
                return None
        except Exception:
            logger.warning("Memory profile read failed; continuing without it", exc_info=True)
            return None
        profile_message = HumanMessage(
            content=rendered.text,
            id=MEMORY_PROFILE_MESSAGE_ID,
            additional_kwargs={
                "hide_from_ui": True,
                MEMORY_PROFILE_KEY: True,
                MEMORY_PROFILE_REVISION_KEY: snapshot.revision,
                MEMORY_PROFILE_HASH_KEY: hashlib.sha256(rendered.text.encode("utf-8")).hexdigest(),
                MEMORY_PROFILE_TOKENS_KEY: rendered.token_count,
                MEMORY_PROFILE_TRUNCATED_KEY: rendered.truncated,
                **provenance_kwargs(ContentKind.MEMORY, "profile_context"),
            },
        )
        if existing_index is not None:
            existing = messages[existing_index]
            if _same_profile_message(existing, profile_message):
                return None
            messages[existing_index] = profile_message
        else:
            target = _latest_real_user_index(messages)
            if target is None:
                return None
            messages.insert(target, profile_message)
        _emit_profile_event(
            snapshot,
            rendered,
            agent_name=_text(context.get("agent_name")) or self._agent_name,
        )
        return {"messages": messages}

    async def _capture_turn_service(
        self, service: MemoryService, state: Mapping, runtime: Runtime
    ) -> None:
        config = self._resolved_config()
        if not config.enabled:
            return
        context = _context(runtime)
        if context.get("skip_memory_flush") is True:
            return
        if _text(context.get("subagent_name")) is not None:
            return
        user_id = _text(context.get("user_id")) or self._user_id
        thread_id = _text(context.get("thread_id"))
        if not user_id or not thread_id:
            return
        messages = list(state.get("messages") or [])
        user_index = _latest_real_user_index(messages)
        if user_index is None:
            return
        assistant_index = _latest_visible_ai_index(messages)
        if assistant_index is None or assistant_index < user_index:
            return
        try:
            await service.capture_turn(
                user_id,
                thread_id,
                _text(context.get("run_id")),
                messages[user_index : assistant_index + 1],
                terminal=False,
            )
            user_text = message_content_to_text(messages[user_index].content)
            if user_text.strip():
                asyncio.create_task(
                    service.update_profile_from_message(user_id, user_text)
                )
        except Exception:
            logger.warning("Failed to capture memory turn", exc_info=True)

    # -- enqueue ------------------------------------------------------------

    def _enqueue_turn(self, state: Mapping, runtime: Runtime) -> None:
        config = self._resolved_config()
        if not config.enabled or not self._resolved_manager().ingestors:
            return
        context = _context(runtime)
        if context.get("skip_memory_flush") is True:
            return
        if _text(context.get("subagent_name")) is not None:
            return
        user_id = _text(context.get("user_id")) or self._user_id
        if not user_id:
            return
        messages = list(state.get("messages") or [])
        user_index = _latest_real_user_index(messages)
        if user_index is None:
            return
        assistant_index = _latest_visible_ai_index(messages)
        if assistant_index is None or assistant_index < user_index:
            return
        window = messages[user_index : assistant_index + 1]
        try:
            self._resolved_manager().ingest(
                MemoryScope(
                    user_id=user_id,
                    agent_name=_text(context.get("agent_name")) or self._agent_name,
                    thread_id=_text(context.get("thread_id")),
                ),
                window,
            )
        except Exception:
            logger.warning("Failed to enqueue memory extraction", exc_info=True)


def _context(runtime: Any) -> dict[str, Any]:
    context = getattr(runtime, "context", None) if runtime is not None else None
    return dict(context) if isinstance(context, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _profile_message_index(messages: list) -> int | None:
    for index, message in enumerate(messages):
        if getattr(message, "id", None) == MEMORY_PROFILE_MESSAGE_ID:
            return index
    return None


def _latest_real_user_index(messages: list) -> int | None:
    return next(
        (index for index in reversed(range(len(messages))) if is_real_user_message(messages[index])),
        None,
    )


def _latest_visible_ai_index(messages: list) -> int | None:
    for index in reversed(range(len(messages))):
        message = messages[index]
        if not isinstance(message, AIMessage) or getattr(message, "tool_calls", None):
            continue
        kwargs = getattr(message, "additional_kwargs", None) or {}
        if kwargs.get("hide_from_ui"):
            continue
        text = message_content_to_text(getattr(message, "content", ""))
        if text.strip():
            return index
    return None


def _same_profile_message(left: Any, right: HumanMessage) -> bool:
    if getattr(left, "content", None) != right.content:
        return False
    left_kwargs = getattr(left, "additional_kwargs", None) or {}
    right_kwargs = right.additional_kwargs or {}
    return left_kwargs.get(MEMORY_PROFILE_REVISION_KEY) == right_kwargs.get(MEMORY_PROFILE_REVISION_KEY)


def _emit_profile_event(snapshot: Any, rendered: Any, *, agent_name: str | None) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        return
    payload = {
        "type": "context:memory",
        "profile_revision": snapshot.revision,
        "content_sha256": hashlib.sha256(rendered.text.encode("utf-8")).hexdigest(),
        "tokens": rendered.token_count,
        "truncated": rendered.truncated,
        "agent_name": agent_name,
    }
    try:
        emit_custom_event(payload, writer=writer)
    except Exception:
        logger.debug("Failed to emit memory context event", exc_info=True)
