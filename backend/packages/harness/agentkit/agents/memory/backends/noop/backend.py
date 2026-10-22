"""No-op backend for disabled deployments and contract tests."""

from __future__ import annotations

from typing import Any, ClassVar

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.types import (
    MemoryBackendCapabilities,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    MemoryStats,
    ScopePage,
)


class NoopMemoryBackend(MemoryBackend):
    capabilities: ClassVar[MemoryBackendCapabilities] = MemoryBackendCapabilities(
        vector_search=False,
        # A no-op backend can still honor the "filter before top-k" contract: there is
        # nothing to return. This keeps the manager's kind registry constructible.
        metadata_filter=True,
        batch_write=False,
        cas=False,
        list_scopes=False,
        ttl=False,
    )

    @classmethod
    def from_config(cls, backend_config: dict[str, Any], *, store: Any, callbacks: Any = None) -> "NoopMemoryBackend":
        del backend_config, store, callbacks
        return cls()

    def get_record(self, scope, kind, record_id):
        return None

    def put_record(self, scope, record, *, expected_revision=None):
        return record

    def put_records(self, scope, records, *, expected_revisions=None):
        return list(records)

    def search_records(self, scope, query):
        return MemorySearchPage()

    def delete_records(self, scope, record_ids, *, mode="soft", reason):
        return 0

    def delete_profile(self, scope):
        return None

    def get_stats(self, scope, *, kind=None):
        return MemoryStats(user_id=scope.user_id)

    def list_scopes(self, *, cursor=None, limit=100):
        return ScopePage()
