"""Procedural memory service (reserved: immutable versions, explicit approval)."""

from __future__ import annotations

import hashlib
import threading
from typing import Literal

from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.agents.memory.types import (
    MemoryKind,
    MemoryKindSpec,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    ProcedurePayload,
    utc_now,
)


class ProceduralMemoryService(MemoryTypeService):
    kind = MemoryKind.PROCEDURAL
    spec = MemoryKindSpec(
        kind=MemoryKind.PROCEDURAL,
        display_name="Procedural memory",
        schema_ref="ProcedurePayload",
        searchable=True,
        injectable=False,
        evictable=False,
        versioned=True,
    )

    def __init__(self, backend, resolver: MemoryResolver, *, ingestors: list | None = None) -> None:
        super().__init__(backend, resolver, ingestors=ingestors)
        self._lock = threading.RLock()

    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        return self.backend.get_record(scope, self.kind, record_id)

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        resolved = query.model_copy(update={"kinds": [self.kind]})
        page = self.backend.search_records(scope, resolved)
        return MemorySearchPage(
            items=[hit for hit in page.items if hit.record.status in {"active", "draft"}],
            next_cursor=page.next_cursor,
        )

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
                    raise ValueError("procedures are managed through versioned proposals")
                payload = ProcedurePayload.model_validate(proposal.payload)
                material = f"{scope.user_id}|{payload.name}|{payload.version}"
                record_id = f"procedure_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
                if self.get(scope, record_id) is not None:
                    continue
                now = utc_now()
                record = MemoryRecord(
                    id=record_id,
                    user_id=scope.user_id,
                    kind=self.kind,
                    subtype="procedure",
                    content=payload.description or payload.name,
                    structured=payload.model_dump(mode="json"),
                    search_text=" ".join([payload.name, payload.description, *payload.steps]),
                    source="import",
                    status=payload.status,
                    created_at=now,
                    updated_at=now,
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
