"""Semantic facts service (M10 section 2.4).

Every write goes through the resolver. The service never injects facts into the
profile prompt; a fact that became a profile field is marked profile_linked and
is excluded from memory_search.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any, Literal
from uuid import uuid4

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.agents.memory.types import (
    MemoryConflict,
    MemoryInstruction,
    MemoryKind,
    MemoryKindSpec,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    SemanticPayload,
    ensure_utc,
    looks_like_secret,
    source_rank,
    utc_now,
)
from agentkit.agents.memory.tokens import count_tokens
from agentkit.privacy import PIIRedactor, contains_redaction_marker, safe_memory_evidence

MAX_FACT_CHARS = 2_000
ALLOWED_CATEGORIES = frozenset({"preference", "project", "context", "profile", "instruction"})
_ALLOWED_SOURCES = frozenset({"user_explicit", "user_confirmed", "inferred", "consolidated", "import"})
_NORMALIZE_RE = re.compile(r"\s+")
logger = logging.getLogger(__name__)


class SemanticMemoryService(MemoryTypeService):
    kind = MemoryKind.SEMANTIC
    spec = MemoryKindSpec(
        kind=MemoryKind.SEMANTIC,
        display_name="Semantic facts",
        schema_ref="SemanticPayload",
        searchable=True,
        injectable=False,
        evictable=True,
    )
    subtype = "fact"

    def __init__(
        self,
        backend: MemoryBackend,
        resolver: MemoryResolver,
        *,
        ingestors: list | None = None,
    ) -> None:
        super().__init__(backend, resolver, ingestors=ingestors)
        self._lock = threading.RLock()

    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None:
        return self.backend.get_record(scope, self.kind, record_id)

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        resolved = query.model_copy(update={"kinds": [self.kind]})
        page = self.backend.search_records(scope, resolved)
        filtered = [
            hit
            for hit in page.items
            if hit.record.status == "active"
            and not _payload(hit.record).profile_linked
            and _payload(hit.record).valid_to is None
        ]
        return MemorySearchPage(
            items=filtered,
            next_cursor=page.next_cursor if len(filtered) == len(page.items) else None,
        )

    def write(
        self,
        scope: MemoryScope,
        proposals: list[MemoryProposal],
        *,
        expected_revision: int | None = None,
    ) -> list[MemoryRecord]:
        written: list[MemoryRecord] = []
        with self._lock:
            for proposal in proposals:
                if proposal.kind != self.kind:
                    continue
                if proposal.operation != "upsert":
                    self._audit_instruction(scope, proposal)
                if proposal.operation == "forget":
                    self.forget(scope, proposal.target_record_id or "", mode="soft")
                    continue
                if proposal.operation == "unprotect":
                    record = self._set_explicit(scope, proposal.target_record_id or "", False)
                    if record is not None:
                        written.append(record)
                    continue
                if proposal.operation == "remember":
                    record = self._set_explicit(scope, proposal.target_record_id or "", True)
                    if record is not None:
                        written.append(record)
                    continue
                if proposal.operation == "supersede":
                    old = self.get(scope, proposal.target_record_id or "")
                    if old is not None:
                        self._mark(old, scope, status="superseded", superseded_by=None)
                    proposal = proposal.model_copy(update={"operation": "upsert"})
                record = self._upsert(scope, proposal)
                if record is not None:
                    written.append(record)
        return written

    def _upsert(self, scope: MemoryScope, proposal: MemoryProposal) -> MemoryRecord | None:
        payload, now = _payload_from_proposal(proposal)
        if self._is_tombstoned(scope, payload):
            logger.info("Dropping a semantic fact that the user explicitly forgot")
            return None
        content = payload.content
        fact_id = _stable_fact_id(scope.user_id, payload.topic_key, content)
        current = self.get(scope, fact_id)

        if current is not None:
            current_payload = _payload(current)
            if _normalized(content) == _normalized(current.content):
                refreshed = current_payload.model_copy(
                    update={
                        "confirmation_count": current_payload.confirmation_count + 1,
                        "last_confirmed_at": now,
                        "confidence": max(current_payload.confidence, payload.confidence),
                        "source": payload.source
                        if source_rank(payload.source) > source_rank(current_payload.source)
                        else current_payload.source,
                        "explicit_remember": current_payload.explicit_remember
                        or payload.explicit_remember,
                    }
                )
                return self._replace(scope, current, refreshed)
            resolution = self.resolver.resolve(self.kind, scope, current, proposal)
            if resolution.action == "reject":
                return None
            if resolution.action == "conflict":
                return self._conflict(scope, current, proposal, payload)
            if resolution.action == "supersede":
                self._mark(current, scope, status="superseded", superseded_by=None)

        topic_matches = self._active_topic(scope, payload.topic_key) if payload.topic_key else []
        if topic_matches:
            authoritative = max(topic_matches, key=lambda item: (source_rank(item.source), ensure_utc(item.updated_at)))
            resolution = self.resolver.resolve(self.kind, scope, authoritative, proposal)
            if resolution.action == "reject":
                return None
            if resolution.action == "conflict":
                return self._conflict(scope, authoritative, proposal, payload)
            for old in topic_matches:
                self._mark(old, scope, status="superseded", superseded_by=fact_id)

        record = _record_from_payload(
            scope,
            payload,
            fact_id=fact_id,
            revision=0,
            now=now,
            evidence=safe_memory_evidence(proposal.evidence),
        )
        return self.backend.put_record(scope, record)

    def _conflict(
        self,
        scope: MemoryScope,
        current: MemoryRecord,
        proposal: MemoryProposal,
        payload: SemanticPayload,
    ) -> MemoryRecord | None:
        candidate_payload = payload.model_copy(update={"status": "conflicted"})
        candidate = _record_from_payload(
            scope,
            candidate_payload,
            fact_id=f"fact_{uuid4().hex}",
            revision=0,
            now=utc_now(),
            evidence=safe_memory_evidence(proposal.evidence),
            status="conflicted",
        )
        stored = self.backend.put_record(scope, candidate)
        conflict = MemoryConflict(
            id=f"conflict_{uuid4().hex}",
            user_id=scope.user_id,
            kind=self.kind,
            subtype=self.subtype,
            topic_key=current.topic_key,
            current_record_id=current.id,
            candidate_record_id=stored.id,
            current_source=current.source,
            candidate_source=payload.source,
            evidence=[safe_memory_evidence(proposal.evidence)]
            if safe_memory_evidence(proposal.evidence)
            else [],
        )
        conflict_record = MemoryRecord(
            id=conflict.id,
            user_id=scope.user_id,
            kind="conflict",
            subtype=self.kind,
            structured=conflict.model_dump(mode="json"),
            source="system",
            created_at=conflict.created_at,
            updated_at=conflict.created_at,
        )
        self.backend.put_record(scope, conflict_record)
        return stored

    def touch(self, scope: MemoryScope, record_id: str) -> None:
        record = self.get(scope, record_id)
        if record is None or record.status != "active":
            return
        payload = _payload(record).model_copy(
            update={
                "access_count": _payload(record).access_count + 1,
                "last_accessed_at": utc_now(),
            }
        )
        self._replace(scope, record, payload)

    def set_explicit(self, scope: MemoryScope, record_id: str, explicit: bool) -> MemoryRecord | None:
        return self._set_explicit(scope, record_id, explicit)

    def _set_explicit(self, scope: MemoryScope, record_id: str, explicit: bool) -> MemoryRecord | None:
        record = self.get(scope, record_id)
        if record is None:
            return None
        payload = _payload(record).model_copy(update={"explicit_remember": explicit})
        return self._replace(scope, record, payload)

    def invalidate_profile_field(self, scope: MemoryScope, field_path: str) -> int:
        changed = 0
        page = self.backend.search_records(
            scope, MemoryQuery(kinds=[self.kind], limit=100)
        )
        for hit in page.items:
            payload = _payload(hit.record)
            if payload.profile_linked == field_path:
                self._replace(
                    scope,
                    hit.record,
                    payload.model_copy(update={"profile_linked": None}),
                    status="superseded",
                )
                changed += 1
        return changed

    def forget(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
    ) -> None:
        self.delete(scope, record_id, mode=mode, reason="user_forget")

    def delete(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str = "user_forget",
    ) -> None:
        if mode == "soft" and reason == "user_forget":
            record = self.get(scope, record_id)
            if record is not None:
                self._write_tombstone(scope, record)
        self.backend.delete_records(scope, [record_id], mode=mode, reason=reason)

    def _audit_instruction(self, scope: MemoryScope, proposal: MemoryProposal) -> None:
        now = utc_now()
        instruction = MemoryInstruction(
            operation=proposal.operation,  # type: ignore[arg-type]
            target_record_id=proposal.target_record_id,
            topic_key=proposal.payload.get("topic_key"),
            evidence=safe_memory_evidence(proposal.evidence),
            source="ui" if proposal.source == "ui" else "conversation",
            created_at=now,
        )
        record = MemoryRecord(
            id=f"instruction_{uuid4().hex}",
            user_id=scope.user_id,
            kind="instruction",
            subtype=self.kind,
            structured=instruction.model_dump(mode="json"),
            source="system",
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.backend.put_record(scope, record)

    def _is_tombstoned(self, scope: MemoryScope, payload: SemanticPayload) -> bool:
        material = "|".join((payload.topic_key or "", _normalized(payload.content)))
        tombstone_id = f"tombstone_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
        return self.backend.get_record(scope, "tombstone", tombstone_id) is not None

    def _write_tombstone(self, scope: MemoryScope, record: MemoryRecord) -> None:
        material = "|".join((record.topic_key or "", _normalized(record.content)))
        tombstone_id = f"tombstone_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
        now = utc_now()
        self.backend.put_record(
            scope,
            MemoryRecord(
                id=tombstone_id,
                user_id=scope.user_id,
                kind="tombstone",
                subtype=self.kind,
                content="",
                structured={
                    "topic_key": record.topic_key,
                    "content_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
                    "cleared_at": now.isoformat(),
                },
                source="user_manual",
                status="active",
                created_at=now,
                updated_at=now,
            ),
        )

    def _active_topic(self, scope: MemoryScope, topic_key: str) -> list[MemoryRecord]:
        page = self.backend.search_records(
            scope,
            MemoryQuery(
                kinds=[self.kind],
                filters={"topic_key": topic_key, "status": "active"},
                limit=100,
            ),
        )
        return [
            hit.record
            for hit in page.items
            if hit.record.topic_key == topic_key and hit.record.status == "active"
        ]

    def _replace(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        payload: SemanticPayload,
        *,
        status: str | None = None,
    ) -> MemoryRecord:
        updated = record.model_copy(
            update={
                "structured": payload.model_dump(mode="json"),
                "status": status or record.status,
                "search_text": payload.content,
                "content": payload.content,
                "confidence": payload.confidence,
                "source": payload.source,
                "topic_key": payload.topic_key,
                "explicit_remember": payload.explicit_remember,
                "updated_at": utc_now(),
            }
        )
        return self.backend.put_record(scope, updated, expected_revision=record.revision)

    def _mark(self, record: MemoryRecord, scope: MemoryScope, *, status: str, superseded_by: str | None) -> None:
        payload = _payload(record).model_copy(update={"status": status})
        updated = record.model_copy(
            update={
                "status": status,
                "superseded_by": superseded_by,
                "structured": payload.model_dump(mode="json"),
                "updated_at": utc_now(),
            }
        )
        self.backend.put_record(scope, updated, expected_revision=record.revision)


def _normalized(value: str) -> str:
    return _NORMALIZE_RE.sub(" ", value.strip().lower())


def _stable_fact_id(user_id: str, topic_key: str | None, content: str) -> str:
    material = "|".join((user_id, topic_key or "", _normalized(content)))
    return f"fact_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _payload(record: MemoryRecord) -> SemanticPayload:
    return SemanticPayload.model_validate(record.structured)


def _payload_from_proposal(proposal: MemoryProposal) -> tuple[SemanticPayload, Any]:
    raw = dict(proposal.payload)
    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("semantic proposal requires non-empty content")
    if len(content) > MAX_FACT_CHARS:
        raise ValueError(f"facts are limited to {MAX_FACT_CHARS} characters")
    if looks_like_secret(content):
        raise ValueError("facts must not contain credentials or identity numbers")
    if contains_redaction_marker(content):
        raise ValueError("facts must not contain redaction markers")
    memory_content = PIIRedactor().redact_for_memory(content)
    if memory_content != content:
        raise ValueError("facts must not contain unredacted PII")
    topic_key = raw.get("topic_key") or None
    if isinstance(topic_key, str) and PIIRedactor().redact_for_memory(topic_key) != topic_key:
        raise ValueError("facts must not contain PII in topic_key")
    source = raw.get("source", "inferred")
    if source not in _ALLOWED_SOURCES:
        raise ValueError(f"unknown semantic source: {source!r}")
    category = raw.get("category") or "context"
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(f"unknown semantic category: {category!r}")
    now = utc_now()
    payload = SemanticPayload(
        content=content.strip(),
        category=category,
        topic_key=raw.get("topic_key") or None,
        confidence=float(raw.get("confidence", proposal.confidence)),
        source=source,
        status="active",
        explicit_remember=bool(raw.get("explicit_remember", False)),
        profile_linked=raw.get("profile_linked"),
        access_count=int(raw.get("access_count", 0)),
        confirmation_count=int(raw.get("confirmation_count", 0)),
        last_accessed_at=raw.get("last_accessed_at"),
        last_confirmed_at=raw.get("last_confirmed_at"),
        valid_from=raw.get("valid_from") or now,
        valid_to=raw.get("valid_to"),
        source_fact_ids=list(raw.get("source_fact_ids") or []),
        fold_depth=int(raw.get("fold_depth", 0)),
    )
    return payload, now


def _record_from_payload(
    scope: MemoryScope,
    payload: SemanticPayload,
    *,
    fact_id: str,
    revision: int,
    now: Any,
    evidence: str,
    status: str = "active",
) -> MemoryRecord:
    return MemoryRecord(
        id=fact_id,
        user_id=scope.user_id,
        kind=MemoryKind.SEMANTIC,
        subtype="fact",
        content=payload.content,
        structured=payload.model_dump(mode="json"),
        search_text=payload.content,
        source=payload.source,
        confidence=payload.confidence,
        status=status,  # type: ignore[arg-type]
        revision=revision,
        created_at=now,
        updated_at=now,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        topic_key=payload.topic_key,
        agent_name=scope.agent_name,
        thread_id=scope.thread_id,
        provenance={"evidence": evidence} if evidence else {},
        explicit_remember=payload.explicit_remember,
        metadata={"tokens": count_tokens(payload.content)},
    )
