"""Type-aware chunking. Every strategy returns the same KnowledgeChunk shape."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentkit.knowledge.config import (
    ChunkingConfig,
    KnowledgeBaseConfig,
)
from agentkit.knowledge.models import KnowledgeChunk
from agentkit.knowledge.tokenizer import TokenCounter, build_token_counter
from agentkit.knowledge.types import KnowledgeBaseType

_FENCE = chr(96) * 3
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_QUESTION_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:Q|问(?:题)?)\s*[:：]\s*(.*)$", re.IGNORECASE)
_API_RE = re.compile(
    r"(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S+|\b(?:class|def|function)\s+\w+)",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(
    r"(?m)^(?=\s*(?:第[一二三四五六七八九十百千万0-9]+条|Article\s+\d+|§\s*\d+|条款\s*\d+))",
    re.IGNORECASE,
)
# 法律法规比安全合规多两类边界：
#   1. 款/项 “（一）”“(1)”——挂在所属“条”下面，不能单独成 chunk，但要在条内断开；
#   2. 编/章/节/分编 的标题行（法律文件的层级比普通文档深）。
# 条本身的识别沿用 _CLAUSE_RE 的写法，另外补 “第X条之一” 这种修订后的条号。
_LEGAL_CLAUSE_RE = re.compile(
    r"(?m)^(?=\s*(?:第[一二三四五六七八九十百千万0-9]+条(?:之[一二三四五六七八九十]+)?"
    r"|Article\s+\d+(?:[.-]\d+)*|§\s*\d+(?:\.\d+)*|条款\s*\d+|\d+\.\d+(?:\.\d+)*\s))",
    re.IGNORECASE,
)
_LEGAL_ARTICLE_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千万0-9]+条(?:之[一二三四五六七八九十]+)?|Article\s+\d+(?:[.-]\d+)*"
    r"|§\s*\d+(?:\.\d+)*|条款\s*\d+|[0-9]+(?:\.[0-9]+)+)",
    re.IGNORECASE,
)
_STEP_RE = re.compile(
    r"^\s*(?:步骤\s*[一二三四五六七八九十百0-9]+|(?:Step)\s*\d+|\d+\s*[.、)])\s*",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _Section:
    path: tuple[str, ...]
    body: str

    @property
    def title(self) -> str:
        return " / ".join(self.path)


def _sections(markdown: str) -> list[_Section]:
    sections: list[_Section] = []
    headings: list[tuple[int, str]] = []
    body: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append(_Section(tuple(title for _, title in headings), text))
        body.clear()

    for line in markdown.split("\n"):
        stripped = line.lstrip()
        is_fence = stripped.startswith((_FENCE, "~~~"))
        if is_fence:
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            body.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            headings[:] = [item for item in headings if item[0] < level]
            headings.append((level, title))
            continue
        body.append(line)
    flush()
    return sections


def _count(counter: TokenCounter, text: str) -> int:
    return counter.count(text)


def _prefix(section: str, max_tokens: int, counter: TokenCounter) -> str:
    if not section:
        return ""
    candidate = section + "\n\n"
    if _count(counter, candidate) > max_tokens // 3:
        candidate = counter.tail(section, max(1, max_tokens // 4)) + "\n\n"
    return candidate


def _split_fenced(units: list[str], separator: str) -> list[str]:
    if separator != "\n\n":
        return units
    merged: list[str] = []
    buffer = ""
    in_fence = False
    for unit in units:
        fence_count = unit.count(_FENCE) + unit.count("~~~")
        if in_fence:
            buffer += unit
            if fence_count % 2:
                in_fence = False
                merged.append(buffer)
                buffer = ""
            continue
        buffer += unit
        if fence_count % 2:
            in_fence = True
            continue
        merged.append(buffer)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def _hard_split(text: str, max_tokens: int, counter: TokenCounter) -> list[str]:
    if _count(counter, text) <= max_tokens:
        return [text]
    for pat in ("\n", "。", "！", "？", ". ", "; ", "；", "，", ", "):
        if pat in text:
            raw = text.split(pat)
            units = [part + (pat if index < len(raw) - 1 else "") for index, part in enumerate(raw)]
            out: list[str] = []
            for unit in units:
                if _count(counter, unit) > max_tokens:
                    out.extend(counter.split(unit, max_tokens))
                else:
                    out.append(unit)
            return [unit for unit in out if unit]
    return counter.split(text, max_tokens)


def _blocks(text: str, separators: list[str], max_tokens: int, counter: TokenCounter) -> list[str]:
    if not text.strip():
        return []
    if _count(counter, text) <= max_tokens:
        return [text]
    for separator in separators:
        if separator and separator in text:
            raw = text.split(separator)
            units = [part + (separator if index < len(raw) - 1 else "") for index, part in enumerate(raw)]
            protected = _split_fenced(units, separator)
            out: list[str] = []
            for unit in protected:
                out.extend(_hard_split(unit, max_tokens, counter) if _count(counter, unit) > max_tokens else [unit])
            return [unit for unit in out if unit]
    return _hard_split(text, max_tokens, counter)


def _pack(
    text: str, section: str, max_tokens: int, overlap_tokens: int,
    separators: list[str], counter: TokenCounter
) -> list[str]:
    prefix = _prefix(section, max_tokens, counter)
    available = max_tokens - _count(counter, prefix)
    if available < max_tokens // 2:
        prefix = ""
        available = max_tokens
    units = _blocks(text, separators, available, counter)
    chunks: list[tuple[str, str]] = []
    current = ""
    previous_body = ""

    def flush() -> None:
        nonlocal current, previous_body
        if current.strip():
            chunks.append((prefix + current.strip(), section))
            previous_body = current.strip()
        current = ""

    for unit in units:
        candidate = current + (("\n" if current else "") + unit)
        if current and _count(counter, candidate) > available:
            flush()
            overlap = counter.tail(previous_body, overlap_tokens) if overlap_tokens else ""
            current = overlap.strip()
            candidate = current + (("\n" if current else "") + unit) if current else unit
            if _count(counter, candidate) > available:
                pieces = counter.split(candidate, available)
                for piece in pieces[:-1]:
                    chunks.append((prefix + piece.strip(), section))
                current = pieces[-1] if pieces else ""
                continue
        current = candidate
    flush()
    return [chunk for chunk in chunks if chunk[0].strip()]


def _pack_groups(
    groups: list[str], section: str, max_tokens: int, overlap_tokens: int,
    separators: list[str], counter: TokenCounter
) -> list[str]:
    del separators
    if not groups:
        return []
    prefix = _prefix(section, max_tokens, counter)
    available = max_tokens - _count(counter, prefix)
    if available < max_tokens // 2:
        prefix = ""
        available = max_tokens
    chunks: list[tuple[str, str]] = []
    current = ""
    for group in groups:
        if _count(counter, group) > available:
            if current.strip():
                chunks.append((prefix + current.strip(), section))
                current = ""
            pieces = _hard_split(group, available, counter)
            chunks.extend((prefix + piece.strip(), section) for piece in pieces[:-1] if piece.strip())
            current = pieces[-1] if pieces else ""
            continue
        candidate = current + (("\n\n" if current else "") + group)
        if current and _count(counter, candidate) > available:
            flushed = current.strip()
            chunks.append((prefix + flushed, section))
            overlap = counter.tail(flushed, overlap_tokens).strip() if overlap_tokens else ""
            if overlap and overlap.count(_FENCE) % 2 == 0:
                candidate = overlap + "\n\n" + group
                current = candidate if _count(counter, candidate) <= available else group
            else:
                current = group
        else:
            current = candidate
    if current.strip():
        chunks.append((prefix + current.strip(), section))
    return [chunk for chunk in chunks if chunk[0].strip()]


def _faq_pairs(sections: list[_Section], fallback: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for section in sections:
        title = section.path[-1] if section.path else ""
        if _QUESTION_RE.match(title) or _QUESTION_RE.match(section.body):
            pairs.append((section.title, section.body))
    if pairs:
        return pairs
    if sections:
        # FAQ documents often use "### 如何..." headings without an explicit Q:.
        # Every leaf section is one question/answer pair.
        return [(section.title, section.body) for section in sections if section.body.strip()]
    matches = list(_QUESTION_RE.finditer(fallback))
    if not matches:
        return []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(fallback)
        block = fallback[match.start():end].strip()
        if block:
            pairs.append((match.group(1).strip(), block))
    return pairs


def _operations_groups(sections: list[_Section], fallback: str) -> list[tuple[str, list[str]]]:
    candidates = sections or [_Section((), fallback)]
    output: list[tuple[str, list[str]]] = []
    for section in candidates:
        groups: list[str] = []
        current: list[str] = []
        in_fence = False
        for line in section.body.split("\n"):
            if line.lstrip().startswith(_FENCE) or line.lstrip().startswith("~~~"):
                in_fence = not in_fence
            if not in_fence and _STEP_RE.match(line) and current:
                groups.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            groups.append("\n".join(current).strip())
        groups = [group for group in groups if group]
        if groups:
            output.append((section.title, groups))
    return output


def _security_clauses(sections: list[_Section], fallback: str) -> list[tuple[str, str]]:
    source = sections or [_Section((), fallback)]
    output: list[tuple[str, str]] = []
    for section in source:
        parts = [part for part in _CLAUSE_RE.split(section.body) if part.strip()]
        if len(parts) <= 1:
            if section.body.strip():
                output.append((section.title, section.body.strip()))
            continue
        for part in parts:
            first_line = part.strip().split("\n", 1)[0]
            clause = first_line.split("：", 1)[0][:80]
            output.append((f"{section.title} / {clause}".strip(" /"), part.strip()))
    return output


def _legal_clauses(sections: list[_Section], fallback: str) -> list[tuple[str, str]]:
    """法律文件按“条”切分；标题里的“第X章/第X节”保留在 section 前面。

    和法律文本打交道的检索问题是“某一条怎么规定的”，所以条是最小单位：
    把相邻两条拼进一个 chunk 会让引用失准。条内的款/项不单独成 chunk，
    只在超长时按通用规则切分，并重复条号。
    """

    source = sections or [_Section((), fallback)]
    output: list[tuple[str, str]] = []
    for section in source:
        parts = [part for part in _LEGAL_CLAUSE_RE.split(section.body) if part.strip()]
        if len(parts) <= 1:
            if section.body.strip():
                output.append((section.title, section.body.strip()))
            continue
        for part in parts:
            body = part.strip()
            match = _LEGAL_ARTICLE_RE.match(body)
            # 条款号进 section：引用时“《XX法》第三条”比“第三章某段”有用得多
            label = match.group(1)[:40] if match else body.split("\n", 1)[0][:80]
            output.append((f"{section.title} / {label}".strip(" /"), body))
    return output


def _api_sections(sections: list[_Section], fallback: str) -> list[_Section]:
    source = sections or [_Section((), fallback)]
    result = [section for section in source if section.body.strip() and _API_RE.search(section.title + "\n" + section.body)]
    if result:
        return result
    return [section for section in source if section.body.strip()]


def chunk_document(
    text: str,
    knowledge_type: KnowledgeBaseType | str,
    config: KnowledgeBaseConfig | ChunkingConfig,
    *,
    token_counter: TokenCounter | None = None,
) -> list[KnowledgeChunk]:
    resolved_type = knowledge_type if isinstance(knowledge_type, KnowledgeBaseType) else KnowledgeBaseType(knowledge_type)
    chunking = config.chunking if isinstance(config, KnowledgeBaseConfig) else config
    type_config = chunking.types[resolved_type.value]
    counter = token_counter
    if counter is None:
        counter = build_token_counter(chunking.tokenizer).counter
    max_tokens = min(type_config.chunk_size_tokens, chunking.max_chunk_tokens)
    overlap = min(type_config.chunk_overlap_tokens, max_tokens - 1)
    sections = _sections(text)
    separators = chunking.separators
    raw_chunks: list[tuple[str, str]] = []

    if resolved_type is KnowledgeBaseType.TECHNICAL:
        if sections:
            for section in sections:
                raw_chunks.extend(_pack(section.body, section.title, max_tokens, overlap, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, overlap, separators, counter))
    elif resolved_type is KnowledgeBaseType.OPERATIONS:
        groups = _operations_groups(sections, text)
        if groups:
            for section, section_groups in groups:
                raw_chunks.extend(_pack_groups(section_groups, section, max_tokens, overlap, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, overlap, separators, counter))
    elif resolved_type is KnowledgeBaseType.FAQ:
        pairs = _faq_pairs(sections, text)
        if pairs:
            for section, pair_text in pairs:
                raw_chunks.extend(_pack(pair_text, section, max_tokens, 0, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, 0, separators, counter))
    elif resolved_type is KnowledgeBaseType.SECURITY:
        clauses = _security_clauses(sections, text)
        if clauses:
            for section, clause_text in clauses:
                raw_chunks.extend(_pack(clause_text, section, max_tokens, overlap, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, overlap, separators, counter))
    elif resolved_type is KnowledgeBaseType.LEGAL:
        legal_clauses = _legal_clauses(sections, text)
        if legal_clauses:
            for section, clause_text in legal_clauses:
                raw_chunks.extend(_pack(clause_text, section, max_tokens, overlap, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, overlap, separators, counter))
    else:
        api_sections = _api_sections(sections, text)
        if api_sections:
            for section in api_sections:
                raw_chunks.extend(_pack(section.body, section.title, max_tokens, 0, separators, counter))
        else:
            raw_chunks.extend(_pack(text, "", max_tokens, 0, separators, counter))

    bounded: list[tuple[str, str]] = []
    for content, section in raw_chunks:
        if _count(counter, content) <= max_tokens:
            bounded.append((content, section))
        else:
            bounded.extend((piece, section) for piece in counter.split(content, max_tokens))

    merged = _merge_small(
        [(content.strip(), section) for content, section in bounded if content.strip()],
        min_tokens=chunking.min_chunk_tokens,
        max_tokens=max_tokens,
        counter=counter,
    )
    chunks: list[KnowledgeChunk] = []
    for content, section in merged:
        if not content:
            continue
        chunks.append(KnowledgeChunk(index=len(chunks), content=content, section=section))
    return chunks


def _merge_small(
    items: list[tuple[str, str]], *, min_tokens: int, max_tokens: int, counter: TokenCounter
) -> list[tuple[str, str]]:
    """Merge tiny adjacent chunks of the same section without breaking the hard cap."""
    if min_tokens <= 0 or len(items) < 2:
        return items
    merged: list[tuple[str, str]] = []
    for content, section in items:
        if not merged:
            merged.append((content, section))
            continue
        previous, previous_section = merged[-1]
        if (
            previous_section == section
            and (_count(counter, previous) < min_tokens or _count(counter, content) < min_tokens)
            and _count(counter, previous + "\n\n" + content) <= max_tokens
        ):
            merged[-1] = (previous + "\n\n" + content, section)
            continue
        merged.append((content, section))
    return merged
