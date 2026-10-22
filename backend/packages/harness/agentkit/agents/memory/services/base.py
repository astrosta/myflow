"""MemoryTypeService contract (M10 section 2.2.4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.types import (
    MemoryKindSpec,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
)


class MemoryTypeService(ABC):
    kind: ClassVar[str]
    spec: ClassVar[MemoryKindSpec]

    def __init__(
        self,
        backend: MemoryBackend,
        resolver: MemoryResolver,
        *,
        ingestors: list | None = None,
    ) -> None:
        self.backend = backend
        self.resolver = resolver
        self.ingestors = list(ingestors or [])

    @abstractmethod
    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        raise NotImplementedError

    @abstractmethod
    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        raise NotImplementedError

    @abstractmethod
    def write(
        self,
        scope: MemoryScope,
        proposals: list[MemoryProposal],
        *,
        expected_revision: int | None = None,
    ) -> list[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str = "user_forget",
    ) -> None:
        raise NotImplementedError
