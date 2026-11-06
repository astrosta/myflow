"""Approximate token counting for chunk boundaries.

Knowledge-base ingestion does not load a model tokenizer. Remote embedding
services do not expose one, and the approximate counter has ample headroom for
the configured chunk sizes, so this module intentionally has no transformers or
Hugging Face dependency.
"""

from __future__ import annotations

import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:['.-][A-Za-z0-9_]+)*|[^\sA-Za-z0-9_]", re.UNICODE)


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...

    def tail(self, text: str, tokens: int) -> str: ...

    def split(self, text: str, max_tokens: int) -> list[str]: ...


class ApproximateTokenCounter:
    """Deterministic word/CJK-character approximation."""

    name = "approximate"

    @staticmethod
    def _matches(text: str) -> list[re.Match[str]]:
        return list(_TOKEN_RE.finditer(text))

    def count(self, text: str) -> int:
        return len(self._matches(text))

    def tail(self, text: str, tokens: int) -> str:
        if tokens <= 0:
            return ""
        matches = self._matches(text)
        if not matches:
            return ""
        start = matches[max(0, len(matches) - tokens)].start()
        return text[start:]

    def split(self, text: str, max_tokens: int) -> list[str]:
        if max_tokens <= 0:
            return [text]
        matches = self._matches(text)
        if len(matches) <= max_tokens:
            return [text]
        parts: list[str] = []
        for offset in range(0, len(matches), max_tokens):
            group = matches[offset : offset + max_tokens]
            start = group[0].start()
            end = group[-1].end()
            parts.append(text[start:end])
        return parts
