"""Token counting for chunk boundaries.

The exact path uses the Qwen tokenizer when transformers is installed and the
tokenizer is available. The fallback is deterministic and flagged, never silently
pretends to be exact.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from agentkit.knowledge.config import TokenizerConfig

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:['.-][A-Za-z0-9_]+)*|[^\sA-Za-z0-9_]", re.UNICODE)


class TokenCounter(Protocol):
    name: str

    def count(self, text: str) -> int: ...

    def tail(self, text: str, tokens: int) -> str: ...

    def split(self, text: str, max_tokens: int) -> list[str]: ...


class ApproximateTokenCounter:
    """Word/CJK-character approximation. Used only when the exact tokenizer is unavailable."""

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


class QwenTokenCounter:
    """Exact Qwen3 tokenizer, loaded lazily so tests and disabled installs need no model."""

    name = "qwen"

    def __init__(self, config: TokenizerConfig) -> None:
        self._config = config
        self._tokenizer = None

    def _load(self):
        if self._tokenizer is None:
            from transformers import AutoTokenizer  # type: ignore[import-not-found]

            source = self._config.path or self._config.model
            self._tokenizer = AutoTokenizer.from_pretrained(
                source,
                local_files_only=self._config.local_files_only,
                trust_remote_code=False,
            )
        return self._tokenizer

    def count(self, text: str) -> int:
        tokenizer = self._load()
        return len(tokenizer.encode(text, add_special_tokens=False))

    def tail(self, text: str, tokens: int) -> str:
        if tokens <= 0:
            return ""
        tokenizer = self._load()
        ids = tokenizer.encode(text, add_special_tokens=False)
        return tokenizer.decode(ids[-tokens:], skip_special_tokens=True)

    def split(self, text: str, max_tokens: int) -> list[str]:
        if max_tokens <= 0:
            return [text]
        tokenizer = self._load()
        ids = tokenizer.encode(text, add_special_tokens=False)
        if len(ids) <= max_tokens:
            return [text]
        return [
            tokenizer.decode(ids[offset : offset + max_tokens], skip_special_tokens=True)
            for offset in range(0, len(ids), max_tokens)
        ]


class ResilientTokenCounter:
    """Use the exact Qwen tokenizer when it loads, fall back on the first failure.

    Loading is deferred until the first chunking call, so an enabled knowledge base
    does not block Gateway startup on a Hugging Face download.
    """

    name = "qwen"

    def __init__(self, primary: TokenCounter) -> None:
        self._primary = primary
        self._fallback = ApproximateTokenCounter()
        self._active: TokenCounter | None = None

    def _counter(self) -> TokenCounter:
        if self._active is not None:
            return self._active
        try:
            self._primary.count("warmup")
        except Exception as exc:  # noqa: BLE001 - optional tokenizer must never break ingestion
            logger.warning(
                "Qwen tokenizer is unavailable (%s); falling back to approximate token counts",
                type(exc).__name__,
            )
            self._active = self._fallback
        else:
            self._active = self._primary
        return self._active

    def count(self, text: str) -> int:
        return self._counter().count(text)

    def tail(self, text: str, tokens: int) -> str:
        return self._counter().tail(text, tokens)

    def split(self, text: str, max_tokens: int) -> list[str]:
        return self._counter().split(text, max_tokens)


@dataclass(frozen=True, slots=True)
class TokenCounterResult:
    counter: TokenCounter
    fallback: bool
    reason: str = ""


def build_token_counter(config: TokenizerConfig) -> TokenCounterResult:
    if config.provider == "approximate":
        return TokenCounterResult(ApproximateTokenCounter(), fallback=True, reason="configured approximate")
    return TokenCounterResult(ResilientTokenCounter(QwenTokenCounter(config)), fallback=False)
