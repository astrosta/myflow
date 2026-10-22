"""MemoryIngestor: extract proposals, never write (M10 section 2.6.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import AIMessage

from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.memory.types import MemoryProposal, MemoryScope
from agentkit.utils.messages import message_content_to_text


class MemoryIngestor(ABC):
    kind: str

    @abstractmethod
    def extract(self, scope: MemoryScope, messages: list[Any]) -> list[MemoryProposal]:
        raise NotImplementedError


class NoopMemoryIngestor(MemoryIngestor):
    def __init__(self, *, kind: str = "semantic") -> None:
        self.kind = kind

    def extract(self, scope: MemoryScope, messages: list[Any]) -> list[MemoryProposal]:
        del scope, messages
        return []


def visible_turn_text(messages: list[Any]) -> tuple[str, str]:
    """Return (all user text, final visible assistant text).

    Hidden injections, tool results, summaries, and internal subagent steps are
    excluded. Only this transcript may be cited as extraction evidence.
    """
    user_parts: list[str] = []
    assistant_parts: list[str] = []
    for message in messages:
        if is_real_user_message(message):
            user_parts.append(message_content_to_text(getattr(message, "content", "")))
            continue
        if isinstance(message, AIMessage):
            kwargs = getattr(message, "additional_kwargs", None) or {}
            if kwargs.get("hide_from_ui"):
                continue
            if getattr(message, "tool_calls", None):
                continue
            text = message_content_to_text(getattr(message, "content", ""))
            if text.strip():
                assistant_parts.append(text)
    assistant = assistant_parts[-1] if assistant_parts else ""
    return "\n".join(user_parts), assistant
