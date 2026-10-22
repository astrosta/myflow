"""Bounded token counting with a deterministic character fallback."""

from __future__ import annotations

import math
from functools import lru_cache


@lru_cache(maxsize=4)
def _encoding(name: str):
    try:
        import tiktoken
    except Exception:
        return None
    try:
        return tiktoken.get_encoding(name)
    except Exception:
        return None


def count_tokens(text: str, *, mode: str = "char", encoding: str = "cl100k_base") -> int:
    """Count tokens for prompt budgets.

    The tiktoken mode is optional. When it is unavailable we use a conservative
    four-characters-per-token estimate so budgets still hold without a network call.
    """
    if not text:
        return 0
    if mode == "tiktoken":
        encoder = _encoding(encoding)
        if encoder is not None:
            return len(encoder.encode(text))
    return max(1, math.ceil(len(text) / 4))
