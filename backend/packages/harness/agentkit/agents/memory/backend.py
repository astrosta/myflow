"""MemoryBackend: the only storage extension point (M10 section 2.2.3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from agentkit.agents.memory.types import (
    MemoryBackendCapabilities,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    MemoryStats,
    ScopePage,
    utc_now,
)


class MemoryBackend(ABC):
    """Store generic MemoryRecord envelopes. Never knows a payload schema."""

    capabilities: ClassVar[MemoryBackendCapabilities] = MemoryBackendCapabilities()

    @abstractmethod
    def get_record(self, scope: MemoryScope, kind: str, record_id: str) -> MemoryRecord | None:
        raise NotImplementedError

    @abstractmethod
    def put_record(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        *,
        expected_revision: int | None = None,
    ) -> MemoryRecord:
        raise NotImplementedError

    def put_records(
        self,
        scope: MemoryScope,
        records: list[MemoryRecord],
        *,
        expected_revisions: dict[str, int] | None = None,
    ) -> list[MemoryRecord]:
        revisions = expected_revisions or {}
        return [
            self.put_record(scope, record, expected_revision=revisions.get(record.id))
            for record in records
        ]

    @abstractmethod
    def search_records(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        raise NotImplementedError

    @abstractmethod
    def delete_records(
        self,
        scope: MemoryScope,
        record_ids: list[str],
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str,
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    def get_stats(self, scope: MemoryScope, *, kind: str | None = None) -> MemoryStats:
        raise NotImplementedError

    def list_scopes(self, *, cursor: str | None = None, limit: int = 100) -> ScopePage:
        raise NotImplementedError

    def delete_profile(self, scope: MemoryScope) -> None:
        """Clear the single profile envelope. Backends may override for a hard delete."""
        raise NotImplementedError

    def close(self) -> None:
        return None

    @classmethod
    @abstractmethod
    def from_config(
        cls,
        backend_config: dict[str, Any],
        *,
        store: Any,
        callbacks: Any = None,
    ) -> "MemoryBackend":
        raise NotImplementedError

    @staticmethod
    def read_failures_are_fatal_for_config() -> bool:
        """Host default: a failed profile read degrades to an empty profile."""
        return False


def touch_record(record: MemoryRecord) -> MemoryRecord:
    """Return a copy with a fresh updated_at; callers own the status fields."""
    return record.model_copy(update={"updated_at": utc_now()})
