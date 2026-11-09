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


_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TITLE_PREFIX_RE = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百千万0-9]+\s*[章节篇]?\s*|[0-9]+(?:\.[0-9]+)*\s*[.、)]?\s*)"
)
_TOC_TITLES = frozenset(
    {
        "目录",
        "目次",
        "目录页",
        "contents",
        "tableofcontents",
    }
)
_NOISE_TITLES = frozenset(
    {
        *_TOC_TITLES,
        "前言", "序言", "序", "preface", "foreword",
        "版权", "版权声明", "版权信息", "版权所有", "copyright", "copyrightnotice",
        "免责声明", "法律声明", "disclaimer", "legalnotice",
        "修订记录", "修订历史", "修订说明", "更新记录", "更新历史",
        "变更记录", "变更历史", "版本历史", "版本记录",
        "revisionhistory", "changelog",
    }
)
_TOC_LINE_RE = re.compile(
    r"^(?=\S).{1,120}?(?:\.{2,}|…{2,}|\t|\s{2,})\s*(?:第\s*)?\d{1,5}\s*(?:页)?\s*$"
)


def _normalise_heading(title: str) -> str:
    value = unicodedata.normalize("NFKC", title).strip()
    value = value.strip("*_ ")
    value = _TITLE_PREFIX_RE.sub("", value)
    return re.sub(r"[\s:：.、，,;；\-—–()（）]+", "", value).lower()


def _is_noise_heading(title: str) -> bool:
    return _normalise_heading(title) in _NOISE_TITLES


def _is_toc_heading(title: str) -> bool:
    return _normalise_heading(title) in _TOC_TITLES


def _strip_noise_sections(text: str) -> str:
    """Remove known non-content sections, preserving same/higher headings after them.

    TOC is special: MinerU often emits `# 目录` followed by `## 第一章…` for both
    the table-of-contents entries and the real body. Blindly skipping every lower
    heading would delete the document. For TOC we therefore stop skipping when:
    - a same/higher heading appears, or
    - a nested heading title repeats (TOC entry then body heading), or
    - non-TOC body text appears under the first chapter heading.
    """
    lines = text.split("\n")
    output: list[str] = []
    skip_level: int | None = None
    toc_mode = False
    toc_seen_headings: set[str] = set()
    pending_toc_heading: str | None = None
    in_fence = False
    fence_marker = ""

    def end_toc_skip() -> None:
        nonlocal skip_level, toc_mode, pending_toc_heading
        skip_level = None
        toc_mode = False
        toc_seen_headings.clear()
        pending_toc_heading = None

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("\x60\x60\x60", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
        heading = None if in_fence else _HEADING_LINE_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            normalised = _normalise_heading(title)
            if skip_level is not None and level <= skip_level:
                end_toc_skip()
            if skip_level is not None:
                if toc_mode:
                    if normalised and normalised in toc_seen_headings:
                        # Repeated chapter title: the real body is starting.
                        end_toc_skip()
                        output.append(line)
                        continue
                    if normalised:
                        toc_seen_headings.add(normalised)
                    pending_toc_heading = line
                continue
            if _is_noise_heading(title):
                skip_level = level
                toc_mode = _is_toc_heading(title)
                toc_seen_headings.clear()
                pending_toc_heading = None
                continue
            output.append(line)
            continue
        if skip_level is None:
            output.append(line)
            continue
        if not toc_mode:
            continue
        if not stripped or _TOC_LINE_RE.match(line):
            continue
        # Real body text under a chapter heading that was listed in the TOC.
        pending = pending_toc_heading
        end_toc_skip()
        if pending:
            output.append(pending)
        output.append(line)
    return "\n".join(output)


def _strip_toc_blocks(text: str) -> str:
    lines = text.split("\n")
    output: list[str] = []
    run: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        nonlocal run
        if len(run) >= 3:
            run = []
            return
        output.extend(run)
        run = []

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("\x60\x60\x60", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
        if not in_fence and _TOC_LINE_RE.match(line):
            run.append(line)
            continue
        flush()
        output.append(line)
    flush()
    return "\n".join(output)


def _dedupe_blocks(text: str) -> str:
    blocks = re.split(r"\n\s*\n", text)
    seen: set[str] = set()
    output: list[str] = []
    for block in blocks:
        candidate = block.strip()
        if not candidate:
            continue
        # Code blocks are semantic content; never dedupe them as boilerplate.
        if "\x60\x60\x60" in candidate or "~~~" in candidate:
            output.append(candidate)
            continue
        normalised = re.sub(r"\s+", " ", candidate).strip().lower()
        if len(normalised) >= 40:
            if normalised in seen:
                continue
            seen.add(normalised)
        output.append(candidate)
    return "\n\n".join(output)


def clean_business_noise(text: str) -> str:
    """Remove deterministic business boilerplate from parsed document text."""
    if not text:
        return ""
    value = _strip_noise_sections(text)
    value = _strip_toc_blocks(value)
    value = _dedupe_blocks(value)
    return value.strip()


def clean_document_text(raw: str) -> str:
    """Full knowledge-base cleaning pipeline: generic normalisation + business noise."""
    return clean_business_noise(clean_text(raw))


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
