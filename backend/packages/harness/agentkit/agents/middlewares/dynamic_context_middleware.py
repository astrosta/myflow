"""Inject the current date beside the latest user message. The system prompt stays static."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from agentkit.agents.middlewares.provenance import ContentKind, provenance_kwargs
from agentkit.config import AppConfig
from agentkit.utils.messages import INJECTED_USER_MESSAGE_ID_SUFFIX

logger = logging.getLogger(__name__)

_INJECT_TIMEOUT_SECONDS = 5.0
_DATE_TIMEZONE_ENV = "MYFLOW_DATE_TIMEZONE"
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"
_REMINDER_DATE_KEY = "reminder_date"
_DATE_RE = re.compile(r"<current_date>([^<]+)</current_date>")
_SUMMARY_MESSAGE_NAME = "summary"
_PROFILE_MESSAGE_ID = "__user_profile__"


def is_dynamic_context_reminder(message: object) -> bool:
    if isinstance(message, dict):
        kwargs = message.get("additional_kwargs")
    else:
        kwargs = getattr(message, "additional_kwargs", None)
    return isinstance(kwargs, dict) and bool(kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY))


def _kwargs(message: object) -> dict:
    if isinstance(message, dict):
        raw = message.get("additional_kwargs")
    else:
        raw = getattr(message, "additional_kwargs", None)
    return raw if isinstance(raw, dict) else {}


def _date_timezone() -> ZoneInfo | None:
    raw = os.environ.get(_DATE_TIMEZONE_ENV, "").strip()
    if not raw:
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("Invalid %s=%r; falling back to the server-local timezone", _DATE_TIMEZONE_ENV, raw)
        return None


def _format_current_date() -> str:
    tz = _date_timezone()
    now = datetime.now(tz) if tz is not None else datetime.now()
    return now.strftime("%Y-%m-%d") + " 星期" + "一二三四五六日"[now.weekday()]


def _full_reminder(current_date: str) -> str:
    return f"<system-reminder>\n<current_date>{current_date}</current_date>\n</system-reminder>"


def _date_update_reminder(current_date: str) -> str:
    return f"<current_date>{current_date}</current_date>"


def _extract_date(content: str) -> str | None:
    match = _DATE_RE.search(content)
    return match.group(1) if match else None


def _message_text(message: object) -> str:
    content = message.content if isinstance(message, SystemMessage) else ""
    return content if isinstance(content, str) else str(content)


def _last_injected_date(messages: list) -> str | None:
    for message in reversed(messages):
        if not is_dynamic_context_reminder(message):
            continue
        structured = _kwargs(message).get(_REMINDER_DATE_KEY)
        if isinstance(structured, str) and structured:
            return structured
        if isinstance(message, SystemMessage):
            found = _extract_date(_message_text(message))
            if found is not None:
                return found
    return None


def _is_user_injection_target(message: object) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    if is_dynamic_context_reminder(message):
        return False
    if message.name == _SUMMARY_MESSAGE_NAME:
        return False
    if message.id == _PROFILE_MESSAGE_ID:
        return False
    if message.id and str(message.id).endswith(INJECTED_USER_MESSAGE_ID_SUFFIX):
        return False
    return True


def _make_reminder_and_user_messages(
    original: HumanMessage,
    reminder_content: str,
    memory_content: str | None = None,
    *,
    reminder_date: str | None = None,
) -> list:
    stable_id = original.id or str(uuid.uuid4())
    reminder_kwargs = {
        "hide_from_ui": True,
        _DYNAMIC_CONTEXT_REMINDER_KEY: True,
        **provenance_kwargs(ContentKind.MIDDLEWARE_INJECTION, "dynamic_context"),
    }
    if reminder_date is not None:
        reminder_kwargs[_REMINDER_DATE_KEY] = reminder_date
    messages: list = [
        SystemMessage(content=reminder_content, id=stable_id, additional_kwargs=reminder_kwargs),
    ]
    if memory_content:
        messages.append(
            HumanMessage(
                content=memory_content,
                id=f"{stable_id}__memory",
                additional_kwargs={
                    "hide_from_ui": True,
                    _DYNAMIC_CONTEXT_REMINDER_KEY: True,
                    **provenance_kwargs(ContentKind.MEMORY, "dynamic_context_memory"),
                },
            )
        )
    messages.append(original.model_copy(update={"id": f"{stable_id}{INJECTED_USER_MESSAGE_ID_SUFFIX}"}))
    return messages


class DynamicContextMiddleware(AgentMiddleware):
    """Date reminder is written back to state. Memory stays empty in this milestone."""

    def __init__(self, agent_name: str | None = None, *, app_config: AppConfig | None = None) -> None:
        super().__init__()
        self._agent_name = agent_name
        self._app_config = app_config

    def _load_memory(self, state, runtime: Runtime | None) -> str | None:
        """M6 leaves the memory slot empty. A later milestone fills this."""
        del state, runtime
        return None

    def _inject(
        self,
        state,
        runtime: Runtime | None = None,
        *,
        current_date: str | None = None,
        memory_content: str | None = None,
    ) -> dict | None:
        del runtime
        messages = list(state.get("messages") or [])
        if not messages:
            return None
        current_date = current_date or _format_current_date()
        last_date = _last_injected_date(messages)
        if last_date == current_date:
            return None
        target_index = next((index for index in reversed(range(len(messages))) if _is_user_injection_target(messages[index])), None)
        if target_index is None:
            return None
        reminder = _full_reminder(current_date) if last_date is None else _date_update_reminder(current_date)
        memory = memory_content if last_date is None else None
        return {
            "messages": _make_reminder_and_user_messages(
                messages[target_index],
                reminder,
                memory,
                reminder_date=current_date,
            )
        }

    def before_agent(self, state, runtime: Runtime) -> dict | None:
        return self._inject(state, runtime, memory_content=self._load_memory(state, runtime))

    async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
        current_date = _format_current_date()
        memory_content = None
        try:
            memory_content = await asyncio.wait_for(
                asyncio.to_thread(self._load_memory, state, runtime),
                timeout=_INJECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "DynamicContextMiddleware: memory injection timed out (%.1fs); injecting the date only",
                _INJECT_TIMEOUT_SECONDS,
            )
        return self._inject(state, runtime, current_date=current_date, memory_content=memory_content)
