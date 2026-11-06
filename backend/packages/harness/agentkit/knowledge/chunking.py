"""Type-aware chunking. Every strategy returns the same KnowledgeChunk shape."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from agentkit.knowledge.config import (
    ChunkingConfig,
    KnowledgeBaseConfig,
)
from agentkit.knowledge.models import KnowledgeChunk
from agentkit.knowledge.tokenizer import ApproximateTokenCounter, TokenCounter
from agentkit.knowledge.types import KnowledgeBaseType

_FENCE = chr(96) * 3
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_QUESTION_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:Q|问(?:题)?)\s*[:：]\s*(.*)$", re.IGNORECASE)

# MinerU often emits legal articles as bold/blockquote/list lines, e.g.
# "**第一条 为保证……**". Strip those Markdown decorations before looking for
# the article number; the number alone is the useful section label.
_LINE_DECORATION = r"(?:[>#*_~`+\-•·\u200b-\u200f\u2060\ufeff]+\s*)*"
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LEADING_DECORATION_RE = re.compile(r"^(?:[>#*_~`+\-•·]+\s*)+")
_TRAILING_DECORATION_RE = re.compile(r"[\s*_~`]+$")
_API_RE = re.compile(
    r"(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S+|\b(?:class|def|function)\s+\w+)",
    re.IGNORECASE,
)
_ARTICLE = (
    r"第\s*[一二三四五六七八九十百千万0-9]+\s*条"
    r"(?:之\s*[一二三四五六七八九十]+\s*)?"
    r"|Article\s+\d+(?:[.-]\d+)*"
    r"|§\s*\d+(?:\.\d+)*"
    r"|条款\s*\d+"
)
_CLAUSE_RE = re.compile(
    rf"(?m)^(?=\s*{_LINE_DECORATION}(?:{_ARTICLE}))",
    re.IGNORECASE,
)
# 法律法规比安全合规多两类边界：
#   1. 款/项 “（一）”“(1)”——挂在所属“条”下面，不能单独成 chunk，但要在条内断开；
#   2. 编/章/节/分编 的标题行（法律文件的层级比普通文档深）。
# 条本身的识别沿用 _CLAUSE_RE 的写法，另外补 “第X条之一” 这种修订后的条号。
_LEGAL_CLAUSE_RE = re.compile(
    rf"(?m)^(?=\s*{_LINE_DECORATION}(?:{_ARTICLE}|\d+\.\d+(?:\.\d+)*\s))",
    re.IGNORECASE,
)
_LEGAL_ARTICLE_RE = re.compile(
    rf"^\s*{_LINE_DECORATION}({_ARTICLE}|\d+\.\d+(?:\.\d+)*)",
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


def _clean_clause_line(value: str) -> str:
    """Strip invisible characters and Markdown decoration around a clause line."""

    text = _INVISIBLE_RE.sub("", value or "").strip()
    text = _LEADING_DECORATION_RE.sub("", text)
    return _TRAILING_DECORATION_RE.sub("", text).strip()


def _clause_label(part: str) -> str:
    """Return only the clause number (第一条 / Article 20 / ...), never the body."""

    first_line = (part or "").strip().split("\n", 1)[0]
    match = _LEGAL_ARTICLE_RE.match(_clean_clause_line(first_line))
    if match is None:
        return ""
    label = match.group(1)[:40]
    if label.startswith("第"):
        label = re.sub(r"\s+", "", label)
    return label


def _section_title(section: _Section) -> str:
    """Keep the heading path, but collapse a heading that is really article text."""

    parts: list[str] = []
    for raw in section.path:
        value = _clean_clause_line(raw)
        label = _clause_label(value)
        if label and len(value) > len(label) + 24:
            value = label
        if value:
            parts.append(value)
    return " / ".join(parts)


def _title_with_clause(title: str, label: str) -> str:
    if not label:
        return title
    if not title:
        return label
    parts = title.split(" / ")
    last = parts[-1]
    if _clause_label(last):
        if last == label:
            return title
        # A heading that was actually article text (e.g. MinerU turned
        # "第一条 …" into a heading) must not accumulate sibling articles.
        parts[-1] = label
        return " / ".join(parts)
    return f"{title} / {label}"


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


def _faq_question(section: _Section) -> str:
    title = section.path[-1] if section.path else ""
    match = _QUESTION_RE.match(title)
    if match:
        value = match.group(1).strip()
        if value:
            return value
    if title and not re.match(r"^(?:常见问题|FAQ|问题列表)$", title, re.IGNORECASE):
        return title
    match = _QUESTION_RE.match(section.body)
    if match:
        value = match.group(1).strip()
        if value:
            return value
    first = section.body.strip().split("\n", 1)[0].strip()
    return first[:200]


def _faq_pairs(sections: list[_Section], fallback: str) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    for section in sections:
        title = section.path[-1] if section.path else ""
        if _QUESTION_RE.match(title) or _QUESTION_RE.match(section.body):
            pairs.append((section.title, section.body, _faq_question(section)))
    if pairs:
        return pairs
    if sections:
        # FAQ documents often use "### 如何..." headings without an explicit Q:.
        # Every leaf section is one question/answer pair.
        return [
            (section.title, section.body, _faq_question(section))
            for section in sections
            if section.body.strip()
        ]
    matches = list(_QUESTION_RE.finditer(fallback))
    if not matches:
        return []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(fallback)
        block = fallback[match.start():end].strip()
        if block:
            pairs.append((match.group(1).strip(), block, match.group(1).strip()))
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
        title = _section_title(section)
        parts = [part for part in _CLAUSE_RE.split(section.body) if part.strip()]
        if len(parts) <= 1:
            if section.body.strip():
                output.append((title, section.body.strip()))
            continue
        for part in parts:
            output.append((_title_with_clause(title, _clause_label(part)), part.strip()))
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
        title = _section_title(section)
        parts = [part for part in _LEGAL_CLAUSE_RE.split(section.body) if part.strip()]
        if len(parts) <= 1:
            if section.body.strip():
                output.append((title, section.body.strip()))
            continue
        for part in parts:
            body = part.strip()
            # 条款号进 section：引用时“《XX法》第三条”比“第三章某段”有用得多。
            # 拿不到条号时宁可不加标签，也不能把整段正文塞进 section。
            output.append((_title_with_clause(title, _clause_label(body)), body))
    return output


def _api_sections(sections: list[_Section], fallback: str) -> list[_Section]:
    source = sections or [_Section((), fallback)]
    result = [section for section in source if section.body.strip() and _API_RE.search(section.title + "\n" + section.body)]
    if result:
        return result
    return [section for section in source if section.body.strip()]


_PARENT_TYPES = frozenset(
    {
        KnowledgeBaseType.OPERATIONS,
        KnowledgeBaseType.SECURITY,
        KnowledgeBaseType.API,
        KnowledgeBaseType.LEGAL,
    }
)
_NO_OVERLAP_TYPES = _PARENT_TYPES


def _parent_text(section: str, content: str) -> str:
    body = content.strip()
    if section and not body.startswith(section):
        return section + "\n\n" + body
    return body


def chunk_document(
    text: str,
    knowledge_type: KnowledgeBaseType | str,
    config: KnowledgeBaseConfig | ChunkingConfig,
    *,
    token_counter: TokenCounter | None = None,
    document_id: str = "document",
) -> list[KnowledgeChunk]:
    resolved_type = knowledge_type if isinstance(knowledge_type, KnowledgeBaseType) else KnowledgeBaseType(knowledge_type)
    chunking = config.chunking if isinstance(config, KnowledgeBaseConfig) else config
    type_config = chunking.types[resolved_type.value]
    counter = token_counter or ApproximateTokenCounter()
    max_tokens = min(type_config.chunk_size_tokens, chunking.max_chunk_tokens)
    overlap = min(type_config.chunk_overlap_tokens, max_tokens - 1)
    sections = _sections(text)
    separators = chunking.separators
    chunks: list[KnowledgeChunk] = []
    parent_seq = 0

    def add_unit(unit: str, section: str, embedding_text: str = "") -> None:
        nonlocal parent_seq
        body = unit.strip()
        if not body:
            return
        if resolved_type in _PARENT_TYPES and _count(counter, body) > max_tokens:
            parent_id = f"{document_id or 'document'}:p{parent_seq}"
            parent_seq += 1
            parent = _parent_text(section, body)
            children = _pack(body, section, max_tokens, 0, separators, counter)
            for child_content, _child_section in children:
                chunks.append(
                    KnowledgeChunk(
                        index=0,
                        content=child_content.strip(),
                        section=section,
                        embedding_text=embedding_text,
                        parent_id=parent_id,
                        parent_content=parent,
                    )
                )
            return
        unit_overlap = 0 if resolved_type in _NO_OVERLAP_TYPES else overlap
        for child_content, child_section in _pack(
            body, section, max_tokens, unit_overlap, separators, counter
        ):
            chunks.append(
                KnowledgeChunk(
                    index=0,
                    content=child_content.strip(),
                    section=child_section,
                    embedding_text=embedding_text,
                )
            )

    if resolved_type is KnowledgeBaseType.TECHNICAL:
        if sections:
            for section in sections:
                add_unit(section.body, section.title)
        else:
            add_unit(text, "")
    elif resolved_type is KnowledgeBaseType.OPERATIONS:
        groups = _operations_groups(sections, text)
        if groups:
            for section, section_groups in groups:
                for group in section_groups:
                    add_unit(group, section)
        else:
            add_unit(text, "")
    elif resolved_type is KnowledgeBaseType.FAQ:
        pairs = _faq_pairs(sections, text)
        if pairs:
            for section, pair_text, question in pairs:
                add_unit(pair_text, section, question)
        else:
            add_unit(text, "")
    elif resolved_type is KnowledgeBaseType.SECURITY:
        clauses = _security_clauses(sections, text)
        if clauses:
            for section, clause_text in clauses:
                add_unit(clause_text, section)
        else:
            add_unit(text, "")
    elif resolved_type is KnowledgeBaseType.LEGAL:
        legal_clauses = _legal_clauses(sections, text)
        if legal_clauses:
            for section, clause_text in legal_clauses:
                add_unit(clause_text, section)
        else:
            add_unit(text, "")
    else:
        api_sections = _api_sections(sections, text)
        if api_sections:
            for section in api_sections:
                add_unit(section.body, section.title)
        else:
            add_unit(text, "")

    # The recursive splitter already respects the hard cap, but keep this
    # defensive bound for unusual counters/separators.
    bounded: list[KnowledgeChunk] = []
    for chunk in chunks:
        if _count(counter, chunk.content) <= max_tokens:
            bounded.append(chunk)
            continue
        for piece in counter.split(chunk.content, max_tokens):
            bounded.append(replace(chunk, content=piece.strip()))
    chunks = [chunk for chunk in bounded if chunk.content]

    if resolved_type is KnowledgeBaseType.TECHNICAL:
        merged = _merge_small(
            [(chunk.content, chunk.section) for chunk in chunks],
            min_tokens=chunking.min_chunk_tokens,
            max_tokens=max_tokens,
            counter=counter,
        )
        chunks = [
            KnowledgeChunk(index=0, content=content, section=section)
            for content, section in merged
        ]

    for index, chunk in enumerate(chunks):
        chunk.index = index
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
