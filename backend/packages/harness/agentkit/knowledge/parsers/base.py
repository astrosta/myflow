"""Parser protocol shared by MinerU and text passthrough."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from agentkit.knowledge.models import ParsedDocument


class DocumentParser(Protocol):
    async def parse(self, path: Path, *, filename: str) -> ParsedDocument: ...

    async def aclose(self) -> None: ...
