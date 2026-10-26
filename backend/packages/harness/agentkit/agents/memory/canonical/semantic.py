"""Semantic fact ingestion, pending review, promotion, and expiry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryRecordView,
    MutationContext,
    PendingPage,
    RecordDraft,
)
from agentkit.agents.memory.canonical.evidence import (
    confidence_for_tier,
    is_active_confidence,
    is_pending_confidence,
)
from agentkit.agents.memory.canonical.proposals import ExistingSemantic, SemanticDocument
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.types import looks_like_secret
from agentkit.config.memory_config import MemoryConfig


@dataclass(frozen=True, slots=True)
class SemanticIngestResult:
    records: list[MemoryRecordView] = field(default_factory=list)
    promoted_ids: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)


class SemanticService:
    def __init__(
        self,
        repo: MemoryRepository,
        operators: Any,
        config: MemoryConfig,
    ) -> None:
        self._repo = repo
        self._operators = operators
        self._config = config

    async def ingest_episodes(
        self,
        user_id: str,
        episode_ids: Sequence[str],
        *,
        source_text: str,
    ) -> SemanticIngestResult:
        existing = await self._repo.find_semantic_existing(
            user_id, topic_key=None, include_pending=True
        )
        existing_payload = [
            ExistingSemantic(id=item.id, document=_document_from_record(item))
            for item in existing
            if _document_from_record(item) is not None
        ]
        extractions = await self._operators.extract_semantic(
            [{"role": "user", "content": source_text}], existing_payload
        )
        by_id = {item.id: item for item in existing}
        by_content = {_normalize(item.content): item for item in existing}
        by_topic = {
            item.topic_key: item
            for item in existing
            if item.topic_key and item.status in {"active", "pending_review"}
        }
        result = SemanticIngestResult()

        for extraction in extractions:
            if extraction.remove:
                current = by_id.get(extraction.id)
                if current is None:
                    continue
                if current.status == "active":
                    updated = await self._repo.update_record(
                        user_id,
                        current.id,
                        expected_revision=current.revision,
                        changes={"status": "deprecated"},
                        mutation=MutationContext("SYSTEM", "semantic-worker", "DELETE", None, ()),
                    )
                    result.records.append(updated)
                continue

            document = extraction.document
            if document is None:
                continue
            if not _valid_document(document, source_text):
                continue
            confidence = confidence_for_tier(document.evidence_tier)
            if confidence < 0.50:
                continue

            current = (
                by_id.get(extraction.id)
                or by_content.get(_normalize(document.content))
                or (by_topic.get(document.topic_key) if document.topic_key else None)
            )
            if current is not None:
                if _normalize(current.content) == _normalize(document.content):
                    if current.status == "pending_review" and is_active_confidence(confidence):
                        promoted = await self._repo.update_record(
                            user_id,
                            current.id,
                            expected_revision=current.revision,
                            changes={"status": "active", "confidence": confidence},
                            mutation=MutationContext(
                                "SYSTEM", "semantic-worker", "UPDATE", "auto_promote", ()
                            ),
                        )
                        result.promoted_ids.append(current.id)
                        result.records.append(promoted)
                    else:
                        result.records.append(current)
                    continue
                if current.status == "active" and is_active_confidence(confidence):
                    conflict = await self._repo.write_conflict(
                        user_id,
                        kind="semantic",
                        topic_key=document.topic_key,
                        current_record_id=current.id,
                        current_snapshot=current.payload,
                        candidate_snapshot=document.model_dump(mode="json"),
                        evidence_refs=[document.evidence_quote],
                    )
                    result.conflict_ids.append(conflict.id)
                    continue

            status = "active" if is_active_confidence(confidence) else "pending_review"
            if not is_active_confidence(confidence) and not is_pending_confidence(confidence):
                continue
            record = await self._repo.create_record(
                RecordDraft(
                    user_id=user_id,
                    id=extraction.id or _record_id(),
                    kind="semantic",
                    subtype="fact",
                    status=status,
                    content=document.content,
                    payload=document.model_dump(mode="json"),
                    topic_key=document.topic_key,
                    scope_condition=document.scope_condition,
                    source="conversation",
                    evidence_tier=document.evidence_tier,
                    confidence=confidence,
                    salience=confidence,
                    explicit_remember=False,
                    parent_memory_id=None,
                    correction_of=None,
                    valid_from=None,
                    valid_to=None,
                    occurred_at=None,
                    thread_id=None,
                    agent_name=None,
                    provenance={"evidence_refs": [document.evidence_quote]},
                    source_episode_ids=tuple(episode_ids),
                ),
                mutation=MutationContext("SYSTEM", "semantic-worker", "ADD", None, tuple(episode_ids)),
            )
            result.records.append(record)
        return result

    async def promote(
        self, user_id: str, record_id: str, *, expected_revision: int
    ) -> MemoryRecordView:
        record = await self._repo.get_record(user_id, record_id)
        if record is None:
            raise MemoryNotFoundError(f"semantic record {record_id} not found")
        if record.status == "expired":
            raise MemoryConflictError("expired semantic record cannot be promoted")
        if record.status != "pending_review":
            return record
        return await self._repo.update_record(
            user_id,
            record_id,
            expected_revision=expected_revision,
            changes={"status": "active"},
            mutation=MutationContext("USER", user_id, "UPDATE", "promote", ()),
        )

    async def expire_pending(self, *, now: datetime) -> int:
        cutoff = now - timedelta(days=self._config.semantic.pending_review_ttl_days)
        rows = await self._repo.list_pending_semantic_before(cutoff)
        for row in rows:
            await self._repo.update_record(
                row.user_id,
                row.id,
                expected_revision=row.revision,
                changes={"status": "expired"},
                mutation=MutationContext("SYSTEM", "semantic-expiry", "UPDATE", "ttl", ()),
            )
        return len(rows)

    async def list_pending(
        self, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> PendingPage:
        return await self._repo.list_pending_semantic(user_id, limit=limit, cursor=cursor)


def _document_from_record(record: MemoryRecordView) -> SemanticDocument | None:
    try:
        return SemanticDocument.model_validate(record.payload)
    except Exception:
        return None


def _valid_document(document: SemanticDocument, source_text: str) -> bool:
    if looks_like_secret(document.content):
        return False
    quote = _normalize(document.evidence_quote)
    return bool(quote) and quote in _normalize(source_text)


def _normalize(value: str) -> str:
    return " ".join(str(value).lower().split())


def _record_id() -> str:
    from uuid import uuid4

    return str(uuid4())

