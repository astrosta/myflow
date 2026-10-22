"""Episodic memory service (reserved in M10: append-only summaries)."""

from __future__ import annotations

import hashlib
import threading
from typing import Literal

from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.agents.memory.types import (
    EpisodicPayload,
    MemoryKind,
    MemoryKindSpec,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    utc_now,
)


class EpisodicMemoryService(MemoryTypeService):
    kind = MemoryKind.EPISODIC
    spec = MemoryKindSpec(
        kind=MemoryKind.EPISODIC,
        display_name="Episodic memory",
        schema_ref="EpisodicPayload",
        searchable=True,
        injectable=False,
        evictable=True,
    )

    def __init__(self, backend, resolver: MemoryResolver, *, ingestors: list | None = None) -> None:
        super().__init__(backend, resolver, ingestors=ingestors)
        self._lock = threading.RLock()

    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        return self.backend.get_record(scope, self.kind, record_id)

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        resolved = query.model_copy(update={"kinds": [self.kind]})
        return self.backend.search_records(scope, resolved)

    def write(
        self,
        scope: MemoryScope,
        proposals: list[MemoryProposal],
        *,
        expected_revision: int | None = None,
    ) -> list[MemoryRecord]:
        del expected_revision
        written: list[MemoryRecord] = []
        with self._lock:
            for proposal in proposals:
                if proposal.kind != self.kind:
                    continue
                if proposal.operation != "upsert":
                    raise ValueError("episodic memory is append-only; destructive instructions are not supported")
                payload = EpisodicPayload.model_validate(proposal.payload)
                material = f"{scope.user_id}|{scope.thread_id or ''}|{payload.summary}"
                record_id = f"episode_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
                if self.get(scope, record_id) is not None:
                    continue
                now = utc_now()
                record = MemoryRecord(
                    id=record_id,
                    user_id=scope.user_id,
                    kind=self.kind,
                    subtype="episode",
                    content=payload.summary,
                    structured=payload.model_dump(mode="json"),
                    search_text=payload.summary,
                    source="conversation",
                    status="active",
                    created_at=now,
                    updated_at=now,
                    valid_from=payload.occurred_at,
                    thread_id=payload.thread_id or scope.thread_id,
                    agent_name=scope.agent_name,
                    provenance={"evidence": proposal.evidence} if proposal.evidence else {},
                )
                written.append(self.backend.put_record(scope, record))
        return written

    def delete(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str = "user_forget",
    ) -> None:
        self.backend.delete_records(scope, [record_id], mode=mode, reason=reason)
