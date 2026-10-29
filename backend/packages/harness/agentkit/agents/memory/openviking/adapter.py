"""Convert LangChain messages into the OpenViking Sessions wire shape.

The adapter is deliberately local and dependency-light. It does not import the
OpenViking SDK, and it performs the final PII/size pass immediately before a
payload can leave the process.
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentkit.agents.memory.openviking.contracts import CaptureRole
from agentkit.privacy import PIIRedactor


class MemoryMessageAdapter:
    def __init__(
        self,
        redactor: PIIRedactor | None = None,
        *,
        max_tool_chars: int = 2_000,
    ) -> None:
        self._redactor = redactor or PIIRedactor()
        self._max_tool_chars = max_tool_chars

    def is_recordable(self, message: Any) -> bool:
        if isinstance(message, SystemMessage):
            return False
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            return False

        kwargs = getattr(message, "additional_kwargs", None) or {}
        if kwargs.get("hide_from_ui"):
            return False
        if kwargs.get("dynamic_context_reminder"):
            return False
        if kwargs.get("memory_recall") or kwargs.get("personalization"):
            return False
        if kwargs.get("memory_profile") or kwargs.get("memory_provenance"):
            return False
        if getattr(message, "name", None) == "summary":
            return False

        text = self._text(message.content)
        if isinstance(message, ToolMessage):
            return bool(text.strip())
        if isinstance(message, AIMessage) and message.tool_calls:
            return True
        return bool(text.strip())

    def to_payloads(self, message: Any) -> list[dict[str, Any]]:
        if not self.is_recordable(message):
            return []

        if isinstance(message, HumanMessage):
            return [
                {
                    "role": "user",
                    "message_kind": "user_query",
                    "parts": [{"type": "text", "text": self._redact(self._text(message.content))}],
                }
            ]

        if isinstance(message, AIMessage):
            parts: list[dict[str, Any]] = []
            text = self._redact(self._text(message.content))
            if text.strip():
                parts.append({"type": "text", "text": text})
            for call in message.tool_calls or []:
                if not isinstance(call, dict):
                    continue
                parts.append(
                    {
                        "type": "tool",
                        "tool_id": str(call.get("id") or ""),
                        "tool_name": str(call.get("name") or ""),
                        "tool_input": self._sanitize_payload(call.get("args")),
                        "tool_status": "pending",
                    }
                )
            return [{"role": "assistant", "message_kind": "assistant_step", "parts": parts}]

        if isinstance(message, ToolMessage):
            raw = self._text(message.content)
            redacted = self._redact(raw)
            truncated = len(redacted) > self._max_tool_chars
            summary = redacted[: self._max_tool_chars]
            tool = {
                "type": "tool",
                "tool_id": str(message.tool_call_id or ""),
                "tool_name": str(message.name or "tool"),
                "tool_output": summary,
                "tool_output_sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
                "tool_output_truncated": truncated,
                "tool_status": "error" if getattr(message, "status", "success") == "error" else "completed",
            }
            duration = (message.additional_kwargs or {}).get("duration_ms")
            if isinstance(duration, (int, float)):
                tool["duration_ms"] = float(duration)
            return [{"role": "assistant", "message_kind": "tool_transport", "parts": [tool]}]

        return []

    def _text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict) and block.get("type") in {"text", "text_block"}:
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
            return "\n".join(chunks)
        return "" if content is None else str(content)

    def _redact(self, text: str) -> str:
        return self._redactor.redact_for_memory(text or "")

    def _sanitize_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._sanitize_payload(item) for item in value]
        if isinstance(value, dict):
            return {key: self._sanitize_payload(item) for key, item in value.items()}
        return value


__all__ = ["MemoryMessageAdapter"]
