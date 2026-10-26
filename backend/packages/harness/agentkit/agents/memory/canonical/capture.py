"""Build bounded turn references from visible chat messages.

The session buffer stores references and short, redacted turn summaries only;
it never persists a raw transcript or a complete tool payload.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.privacy import PIIRedactor
from agentkit.utils.messages import message_content_to_text

_PROFILE_MESSAGE_ID = "__user_profile__"


@dataclass(frozen=True, slots=True)
class TurnRef:
    message_id: str
    role: str
    run_id: str | None
    tool_name: str | None
    tool_status: str | None
    summary: str
    text_sha256: str


def build_turn_refs(
    messages: Sequence[Any],
    *,
    run_id: str | None,
    max_summary_chars: int = 600,
) -> list[TurnRef]:
    refs: list[TurnRef] = []
    seen: set[str] = set()
    redactor = PIIRedactor()
    for index, message in enumerate(messages):
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            continue
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, str) or not message_id:
            message_id = hashlib.sha256(
                f"{run_id or ''}:{index}:{type(message).__name__}".encode("utf-8")
            ).hexdigest()[:32]
        if message_id == _PROFILE_MESSAGE_ID or message_id in seen:
            continue
        kwargs = getattr(message, "additional_kwargs", None) or {}
        if kwargs.get("hide_from_ui"):
            continue
        text = message_content_to_text(getattr(message, "content", "")) or ""
        role = "user" if isinstance(message, HumanMessage) else "assistant"
        tool_name = None
        tool_status = None
        if isinstance(message, ToolMessage):
            role = "tool"
            tool_name = getattr(message, "name", None) or "tool"
            tool_status = "ok"
        cleaned = redactor.redact_for_memory(text)
        if role == "tool":
            summary = f"tool {tool_name} ({tool_status}); {cleaned}"
        else:
            summary = f"{role}: {cleaned}"
        summary = summary.strip()[:max_summary_chars]
        if not summary:
            continue
        refs.append(
            TurnRef(
                message_id=message_id,
                role=role,
                run_id=run_id,
                tool_name=tool_name,
                tool_status=tool_status,
                summary=summary,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
        seen.add(message_id)
    return refs


_REMEMBER = re.compile(
    r"(记住(?:这次|这个|一下)?|总结一下|remember\s+this|save\s+this)", re.IGNORECASE
)


def is_explicit_remember(text: str) -> bool:
    """Deterministic fast path; no model is consulted."""
    return bool(_REMEMBER.search(text or ""))
