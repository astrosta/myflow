"""Deterministic tokenizer for BM25: ASCII terms plus CJK bigrams."""

from __future__ import annotations

import re

_ASCII_TOKEN_RE = re.compile("[a-z0-9_]+(?:[.\\-/+][a-z0-9_]+)*")
_CJK_RUN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """Split text into lower-case ASCII terms and overlapping CJK bigrams.

    No external tokenizer is required. A one-character CJK run is kept as-is;
    longer runs produce bigrams so a query such as "租户" can still hit
    "多租户".
    """
    lowered = text.lower()
    tokens = _ASCII_TOKEN_RE.findall(lowered)
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


__all__ = ["tokenize"]
