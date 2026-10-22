"""Deterministic text cleaning shared by every parser and chunking strategy."""

from __future__ import annotations

import html
import re
import unicodedata

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)", re.IGNORECASE)
_HTML_BREAK_RE = re.compile(r"<\s*(?:br|hr)\s*/?\s*>", re.IGNORECASE)
_HTML_BLOCK_RE = re.compile(r"</?\s*(?:p|div|li|tr|h[1-6]|table|section|article)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:[-—–]\s*)?(?:第\s*)?\d{1,5}\s*(?:页)?\s*(?:[-—–])?\s*$")


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    value = unicodedata.normalize("NFKC", value)
    value = value.replace(chr(0xFEFF), "").replace(chr(0x200B), "")
    value = _SCRIPT_RE.sub("", value)
    value = _COMMENT_RE.sub("", value)
    value = _IMAGE_RE.sub("", value)
    value = _HTML_BREAK_RE.sub("\n", value)
    value = _HTML_BLOCK_RE.sub("\n", value)
    value = _HTML_TAG_RE.sub("", value)
    value = html.unescape(value)
    value = "".join(
        char for char in value
        if char == "\n" or char == "\t" or not (ord(char) < 32 or 0x7F <= ord(char) < 0xA0)
    )
    lines: list[str] = []
    blank = 0
    for line in value.split("\n"):
        line = line.rstrip(" \t")
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
            lines.append("")
            continue
        blank = 0
        if _PAGE_NUMBER_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
