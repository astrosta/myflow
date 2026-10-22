"""Text PII redaction and run-scoped token vault.

The vault is the only place where a run's original sensitive value is kept. It is
deliberately not serializable and must never be written to state or checkpoints.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

TOKEN_PREFIX = "[[PII:"
TOKEN_RE = re.compile(r"\[\[PII:([A-Z_]+):([0-9a-f]{8}):(\d+)\]\]")
REDACTED = "[REDACTED]"


class PIIUnresolvedToken(RuntimeError):
    """A tool received a redaction handle that this run cannot resolve."""


@dataclass
class PIIVault:
    """Run-scoped, in-memory original-value store. Never serialize this object."""

    vault_id: str = field(default_factory=lambda: secrets.token_hex(4))
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _by_token: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _by_value: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)

    def mint(self, kind: str, value: str) -> str:
        key = (kind, value)
        with self._lock:
            existing = self._by_value.get(key)
            if existing is not None:
                return existing
            self._sequence += 1
            token = f"{TOKEN_PREFIX}{kind}:{self.vault_id}:{self._sequence}]]"
            self._by_value[key] = token
            self._by_token[token] = value
            return token

    def resolve(self, token: str) -> str | None:
        with self._lock:
            return self._by_token.get(token)

    def failsafe(self, kind: str = "FAILSAFE") -> str:
        with self._lock:
            self._sequence += 1
            return f"{TOKEN_PREFIX}{kind}:{self.vault_id}:{self._sequence}]]"

    def clear(self) -> None:
        with self._lock:
            self._by_token.clear()
            self._by_value.clear()


Pattern = tuple[str, re.Pattern[str]]
_PATTERNS: tuple[Pattern, ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("AUTH", re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+")),
    ("BEARER", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("URL_CREDENTIALS", re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|mongodb)://[^\s/@:]+:[^@\s/]+@[^\s]+")),
    ("CREDENTIAL", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie)\s*(?:is|=|:)\s*[\"']?[^\s\"',;]{4,}")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CN_ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"(?<!\d)(?:\d{4}[- ]?){3}\d{4}(?!\d)")),
)


class PIIRedactor:
    """Deterministic best-effort text redaction with a run-scoped token vault."""

    def __init__(self, vault: PIIVault | None = None) -> None:
        self.vault = vault or PIIVault()

    def redact_text(self, text: str, *, kind: str | None = None) -> str:
        if not text:
            return text
        try:
            # Preserve existing handles; only process literal text around them.
            result: list[str] = []
            cursor = 0
            for match in TOKEN_RE.finditer(text):
                result.append(self._redact_segment(text[cursor : match.start()], kind, None))
                result.append(match.group(0))
                cursor = match.end()
            result.append(self._redact_segment(text[cursor:], kind, None))
            return "".join(result)
        except Exception:
            # Fail closed with an unresolvable placeholder; never return the raw text.
            return self.vault.failsafe()

    def redact_for_memory(self, text: str) -> str:
        """Irreversibly remove handles and literal PII. Never returns a handle."""
        if not text:
            return text
        try:
            text = TOKEN_RE.sub(REDACTED, text)
            for _kind, pattern in _PATTERNS:
                text = pattern.sub(REDACTED, text)
            return text
        except Exception:
            return REDACTED

    def _redact_segment(self, text: str, kind: str | None, replacement: str | None) -> str:
        if not text:
            return text
        for current_kind, pattern in _PATTERNS:
            selected_kind = kind or current_kind
            text = pattern.sub(
                lambda match, selected_kind=selected_kind: (
                    replacement if replacement is not None else self.vault.mint(selected_kind, match.group(0))
                ),
                text,
            )
        return text

    def redact_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_payload(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_payload(item) for item in value)
        if isinstance(value, dict):
            return {key: self.redact_payload(item) for key, item in value.items()}
        return value

    def restore_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._restore_text(value)
        if isinstance(value, list):
            return [self.restore_payload(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.restore_payload(item) for item in value)
        if isinstance(value, dict):
            return {key: self.restore_payload(item) for key, item in value.items()}
        return value

    def _restore_text(self, text: str) -> str:
        def resolve(match: re.Match[str]) -> str:
            original = self.vault.resolve(match.group(0))
            if original is None:
                raise PIIUnresolvedToken(
                    f"PII handle {match.group(1)} is not resolvable in this run"
                )
            return original

        return TOKEN_RE.sub(resolve, text)


def has_redaction_marker(text: str) -> bool:
    return bool(TOKEN_RE.search(text)) or REDACTED in text


def redact_raw_input(raw: Any, redactor: PIIRedactor) -> Any:
    """Redact HumanMessage payloads in the raw input dict without changing shape."""
    if not isinstance(raw, dict):
        return raw
    messages = raw.get("messages")
    if not isinstance(messages, list):
        return raw
    redacted = []
    for item in messages:
        if isinstance(item, str):
            redacted.append(redactor.redact_text(item))
            continue
        if not isinstance(item, dict):
            redacted.append(item)
            continue
        role = item.get("role") or item.get("type")
        if role not in {"human", "user"}:
            redacted.append(item)
            continue
        updated = dict(item)
        if "content" in updated:
            updated["content"] = _redact_content(updated["content"], redactor)
        redacted.append(updated)
    return {**raw, "messages": redacted}


def redact_message_content(content: Any, redactor: PIIRedactor) -> Any:
    return _redact_content(content, redactor)


def memory_view_content(content: Any, redactor: PIIRedactor) -> Any:
    if isinstance(content, str):
        return redactor.redact_for_memory(content)
    if isinstance(content, list):
        return [
            {**block, "text": redactor.redact_for_memory(str(block.get("text") or ""))}
            if isinstance(block, dict) and block.get("type") == "text"
            else block
            for block in content
        ]
    return content


def _redact_content(content: Any, redactor: PIIRedactor) -> Any:
    if isinstance(content, str):
        return redactor.redact_text(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, str):
                result.append(redactor.redact_text(block))
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                result.append({**block, "text": redactor.redact_text(block["text"])})
            else:
                result.append(block)
        return result
    return content



def redact_human_messages(messages: list[Any], redactor: PIIRedactor) -> list[Any]:
    """Return message copies with genuine HumanMessage text redacted.

    Hidden profile/date injections and summaries are not user input and must not be
    tokenized (the profile allowlist is deliberately exempt).
    """
    from agentkit.agents.middlewares.message_utils import is_genuine_user_message

    result = list(messages)
    for index, message in enumerate(result):
        if not is_genuine_user_message(message):
            continue
        content = getattr(message, "content", None)
        redacted = redact_message_content(content, redactor)
        kwargs = redactor.redact_payload(dict(getattr(message, "additional_kwargs", None) or {}))
        if redacted != content or kwargs != (getattr(message, "additional_kwargs", None) or {}):
            result[index] = message.model_copy(
                update={"content": redacted, "additional_kwargs": kwargs}
            )
    return result


def redact_ai_messages(messages: list[Any], redactor: PIIRedactor) -> list[Any]:
    """Redact model output before it is written back to state/checkpoint."""
    from langchain_core.messages import AIMessage

    from agentkit.agents.middlewares.message_utils import clone_ai_message_with_tool_calls

    result = list(messages)
    for index, message in enumerate(result):
        if not isinstance(message, AIMessage):
            continue
        content = getattr(message, "content", None)
        redacted_content = redact_message_content(content, redactor)
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        redacted_calls = []
        for call in tool_calls:
            if not isinstance(call, dict):
                redacted_calls.append(call)
                continue
            args = call.get("args")
            redacted_calls.append({**call, "args": redactor.redact_payload(args)})
        kwargs = dict(getattr(message, "additional_kwargs", None) or {})
        redacted_kwargs = redactor.redact_payload(kwargs)
        if (
            redacted_content != content
            or redacted_calls != tool_calls
            or redacted_kwargs != kwargs
        ):
            updated = clone_ai_message_with_tool_calls(message, redacted_calls)
            result[index] = updated.model_copy(
                update={"content": redacted_content, "additional_kwargs": redacted_kwargs}
            )
    return result


def memory_view_messages(messages: list[Any], redactor: PIIRedactor | None = None) -> list[Any]:
    """Return an irreversible redacted view for memory extraction.

    Handles and raw PII both become [REDACTED]. This view must never be written to a
    checkpoint; it is only fed to MemoryIngestor.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    redactor = redactor or PIIRedactor()
    result = list(messages)
    for index, message in enumerate(result):
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            continue
        content = getattr(message, "content", None)
        redacted = memory_view_content(content, redactor)
        if redacted != content:
            result[index] = message.model_copy(update={"content": redacted})
    return result


def contains_redaction_marker(text: str) -> bool:
    return has_redaction_marker(text)


def safe_memory_evidence(text: str | None) -> str:
    """Evidence is model-visible and persisted; never retain PII or handles there."""
    if not text:
        return ""
    cleaned = PIIRedactor().redact_for_memory(text)
    return "" if has_redaction_marker(cleaned) else cleaned
