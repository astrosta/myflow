"""Plain text / Markdown / HTML passthrough. No external parser call."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.models import ParsedDocument

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


class LocalTextParser:
    async def parse(self, path: Path, *, filename: str) -> ParsedDocument:
        raw = await asyncio.to_thread(path.read_bytes)
        decoded: str | None = None
        encoding = ""
        for candidate in _ENCODINGS:
            try:
                decoded = raw.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if decoded is None or "\x00" in decoded[:4096]:
            raise KnowledgeUnavailable(f"Unable to decode text document {filename!r}")
        return ParsedDocument(markdown=decoded, metadata={"encoding": encoding, "parser": "local-text"})

    async def aclose(self) -> None:
        return None
