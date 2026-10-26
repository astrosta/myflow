"""PostgreSQL transaction boundary for canonical memory.

Every multi-row mutation writes the business row, an immutable version (when
applicable), a ledger row, and an outbox row in one transaction.  This module
never calls an embedding provider or Milvus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryConflictView,
    SkillCandidateView,
    MemoryIngestJobView,
    MemoryLedgerView,
    MemoryNotFoundError,
    MemoryOutboxView,
    MemoryProfileView,
    MemoryRecordView,
    MemorySessionView,
    MutationContext,
    PendingPage,
    RecordDraft,
)
from agentkit.agents.memory.canonical.models import (
    MemoryConflictRow,
    MemoryEpisodeSessionRow,
    MemorySkillCandidateRow,
    MemoryIndexOutboxRow,
    MemoryIngestJobRow,
    MemoryMutationLedgerRow,
    MemoryProfileRow,
    MemoryRecordRow,
    MemoryRecordVersionRow,
    MemoryScopeMetaRow,
)

_PROJECTED_KINDS = frozenset({"semantic", "episodic"})


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryRepository:
    """SQLAlchemy repository. It is the only canonical writer."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # -- profiles -----------------------------------------------------------

    async def get_profile(self, user_id: str) -> MemoryProfileView | None:
        async with self._sf() as session:
            row = await session.get(MemoryProfileRow, user_id)
            return _profile_view(row) if row is not None else None

    async def ensure_profile(self, user_id: str) -> MemoryProfileView:
        async with self._sf() as session:
            row = await session.get(MemoryProfileRow, user_id)
            if row is None:
                row = MemoryProfileRow(
                    user_id=user_id,
                    schema_version=1,
                    revision=0,
                    profile={},
                    tombstones={},
                    updated_at=_now(),
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    row = await session.get(MemoryProfileRow, user_id)
            return _profile_view(row)

    async def cas_profile(
        self,
        user_id: str,
        *,
        expected_revision: int | None,
        profile: dict[str, Any],
        tombstones: dict[str, Any],
        mutation: MutationContext,
    ) -> MemoryProfileView:
        async with self._sf() as session:
            row = await session.get(MemoryProfileRow, user_id)
            if row is None:
                row = MemoryProfileRow(
                    user_id=user_id,
                    schema_version=1,
                    revision=0,
                    profile={},
                    tombstones={},
                    updated_at=_now(),
                )
                session.add(row)
                await session.flush()
            current = int(row.revision)
            if expected_revision is not None and expected_revision != current:
                raise MemoryConflictError(
                    f"memory profile changed (expected revision {expected_revision}, found {current})",
                    current_revision=current,
                )
            before = {"profile": row.profile, "tombstones": row.tombstones}
            row.profile = profile
            row.tombstones = tombstones
            row.revision = current + 1
            row.updated_at = _now()
            _ledger(
                session,
                user_id=user_id,
                mutation=mutation,
                before_id=None,
                after_id=None,
                before_revision=current,
                after_revision=row.revision,
                before_status=None,
                after_status="active",
                op_json={"profile": profile, "tombstones": tombstones},
                inverse_json=before,
            )
            await session.commit()
            return _profile_view(row)

    # -- records ------------------------------------------------------------

    async def create_record(
        self, draft: RecordDraft, *, mutation: MutationContext
    ) -> MemoryRecordView:
        now = _now()
        async with self._sf() as session:
            row = MemoryRecordRow(
                id=draft.id,
                user_id=draft.user_id,
                kind=draft.kind,
                subtype=draft.subtype,
                status=draft.status,
                revision=0,
                content=draft.content,
                payload=draft.payload,
                topic_key=draft.topic_key,
                scope_condition=draft.scope_condition,
                source=draft.source,
                evidence_tier=draft.evidence_tier,
                confidence=draft.confidence,
                consolidation_status="pending" if draft.kind == "episodic" else None,
                salience=draft.salience,
                hit_count=0,
                last_active_at=now,
                explicit_remember=draft.explicit_remember,
                parent_memory_id=draft.parent_memory_id,
                correction_of=draft.correction_of,
                valid_from=draft.valid_from,
                valid_to=draft.valid_to,
                occurred_at=draft.occurred_at,
                thread_id=draft.thread_id,
                agent_name=draft.agent_name,
                provenance=draft.provenance,
                source_episode_ids=list(draft.source_episode_ids),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(
                MemoryRecordVersionRow(
                    memory_id=draft.id,
                    user_id=row.user_id,
                    revision=0,
                    status=draft.status,
                    content=draft.content,
                    payload=draft.payload,
                    valid_from=draft.valid_from,
                    valid_to=draft.valid_to,
                    changed_at=now,
                )
            )
            _ledger(
                session,
                user_id=row.user_id,
                mutation=mutation,
                before_id=None,
                after_id=row.id,
                before_revision=None,
                after_revision=0,
                before_status=None,
                after_status=row.status,
                op_json={"content": row.content, "payload": row.payload},
                inverse_json={},
            )
            if _projects(row):
                await _enqueue_outbox(session, row, op="upsert")
            await session.commit()
            return _record_view(row)

    async def update_record(
        self,
        user_id: str,
        record_id: str,
        *,
        expected_revision: int,
        changes: dict[str, Any],
        mutation: MutationContext,
    ) -> MemoryRecordView:
        allowed = {
            "status",
            "content",
            "payload",
            "topic_key",
            "scope_condition",
            "confidence",
            "salience",
            "explicit_remember",
            "valid_from",
            "valid_to",
            "archived_at",
            "deleted_at",
            "consolidation_status",
            "consolidated_at",
            "source_episode_ids",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported memory fields: {sorted(unknown)}")
        async with self._sf() as session:
            row = await session.get(MemoryRecordRow, record_id)
            if row is None or row.user_id != user_id:
                raise MemoryNotFoundError(f"memory record {record_id} not found")
            if int(row.revision) != expected_revision:
                raise MemoryConflictError(
                    f"memory record changed (expected revision {expected_revision}, found {row.revision})",
                    current_revision=int(row.revision),
                )
            before_status = row.status
            before = {"status": row.status, "content": row.content, "payload": row.payload}
            for key, value in changes.items():
                setattr(row, key, value)
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
            session.add(
                MemoryRecordVersionRow(
                    memory_id=row.id,
                    user_id=user_id,
                    revision=row.revision,
                    status=row.status,
                    content=row.content,
                    payload=row.payload,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                    changed_at=row.updated_at,
                )
            )
            _ledger(
                session,
                user_id=user_id,
                mutation=mutation,
                before_id=row.id,
                after_id=row.id,
                before_revision=expected_revision,
                after_revision=row.revision,
                before_status=before_status,
                after_status=row.status,
                op_json=changes,
                inverse_json=before,
            )
            if _projects(row):
                await _enqueue_outbox(session, row, op="upsert")
            elif before_status == "active":
                await _enqueue_outbox(session, row, op="delete")
            await session.commit()
            return _record_view(row)

    async def get_record(self, user_id: str, record_id: str) -> MemoryRecordView | None:
        async with self._sf() as session:
            row = await session.get(MemoryRecordRow, record_id)
            if row is None or row.user_id != user_id:
                return None
            return _record_view(row)

    async def get_record_unscoped(self, record_id: str) -> MemoryRecordView | None:
        """Internal lookup for services that already checked ownership."""
        async with self._sf() as session:
            row = await session.get(MemoryRecordRow, record_id)
            return _record_view(row) if row is not None else None

    async def find_semantic_existing(
        self,
        user_id: str,
        *,
        topic_key: str | None,
        include_pending: bool = True,
    ) -> list[MemoryRecordView]:
        statuses = ["active"] + (["pending_review"] if include_pending else [])
        async with self._sf() as session:
            statement = select(MemoryRecordRow).where(
                MemoryRecordRow.user_id == user_id,
                MemoryRecordRow.kind == "semantic",
                MemoryRecordRow.status.in_(statuses),
            )
            if topic_key is not None:
                statement = statement.where(MemoryRecordRow.topic_key == topic_key)
            rows = (await session.scalars(statement)).all()
            return [_record_view(row) for row in rows]

    async def get_epoch(self, user_id: str) -> int:
        async with self._sf() as session:
            row = await session.get(MemoryScopeMetaRow, user_id)
            return int(row.epoch) if row is not None else 0

    async def search_lexical(
        self,
        user_id: str,
        *,
        text: str,
        kinds: Sequence[str],
        limit: int = 50,
    ) -> list[tuple[MemoryRecordView, float]]:
        import re

        terms = set(re.findall(r"[\w-]+", text.lower(), re.UNICODE))
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(MemoryRecordRow)
                    .where(
                        MemoryRecordRow.user_id == user_id,
                        MemoryRecordRow.status == "active",
                        MemoryRecordRow.kind.in_(tuple(kinds)),
                    )
                    .order_by(MemoryRecordRow.updated_at.desc())
                    .limit(1000)
                )
            ).all()
        scored: list[tuple[MemoryRecordView, float]] = []
        for row in rows:
            view = _record_view(row)
            content = f"{view.content} {view.topic_key or ''}".lower()
            body_terms = set(re.findall(r"[\w-]+", content, re.UNICODE))
            overlap = len(terms & body_terms)
            substring = 0.25 if text.strip().lower() in content else 0.0
            score = (overlap / max(1, len(terms))) + substring
            if score > 0.0 or not terms:
                scored.append((view, score))
        scored.sort(key=lambda item: (-item[1], item[0].updated_at), reverse=False)
        return scored[:limit]

    async def list_active_records(
        self, user_id: str | None = None, *, limit: int = 1000
    ) -> list[MemoryRecordView]:
        async with self._sf() as session:
            statement = select(MemoryRecordRow).where(
                MemoryRecordRow.status == "active",
                MemoryRecordRow.kind.in_(("semantic", "episodic")),
            )
            if user_id is not None:
                statement = statement.where(MemoryRecordRow.user_id == user_id)
            rows = (
                await session.scalars(
                    statement.order_by(MemoryRecordRow.updated_at.asc()).limit(limit)
                )
            ).all()
            return [_record_view(row) for row in rows]

    async def list_records(
        self,
        user_id: str,
        *,
        kind: str,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[MemoryRecordView]:
        async with self._sf() as session:
            statement = select(MemoryRecordRow).where(
                MemoryRecordRow.user_id == user_id,
                MemoryRecordRow.kind == kind,
            )
            if statuses is not None:
                statement = statement.where(MemoryRecordRow.status.in_(statuses))
            rows = (
                await session.scalars(
                    statement.order_by(MemoryRecordRow.updated_at.desc()).limit(limit)
                )
            ).all()
            return [_record_view(row) for row in rows]

    async def list_pending_semantic(
        self, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> PendingPage:
        offset = 0
        if cursor:
            try:
                offset = max(0, int(cursor))
            except ValueError as exc:
                raise ValueError("invalid pending cursor") from exc
        rows = await self.list_records(
            user_id, kind="semantic", statuses=["pending_review"], limit=offset + limit + 1
        )
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if len(rows) > offset + limit else None
        return PendingPage(items=page, next_cursor=next_cursor)

    async def list_pending_semantic_before(
        self, cutoff: datetime, *, limit: int = 100
    ) -> list[MemoryRecordView]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(MemoryRecordRow)
                    .where(
                        MemoryRecordRow.kind == "semantic",
                        MemoryRecordRow.status == "pending_review",
                        MemoryRecordRow.updated_at < cutoff,
                    )
                    .order_by(MemoryRecordRow.updated_at.asc())
                    .limit(limit)
                )
            ).all()
            return [_record_view(row) for row in rows]

    async def soft_delete(
        self,
        user_id: str,
        record_id: str,
        *,
        reason: str,
        mutation: MutationContext,
    ) -> MemoryRecordView:
        current = await self.get_record(user_id, record_id)
        if current is None:
            raise MemoryNotFoundError(f"memory record {record_id} not found")
        return await self.update_record(
            user_id,
            record_id,
            expected_revision=current.revision,
            changes={"status": "deleted", "deleted_at": _now()},
            mutation=MutationContext(
                mutation.actor_type,
                mutation.actor_id,
                "DELETE",
                reason,
                mutation.source_episode_ids,
            ),
        )

    async def rollback(
        self,
        user_id: str,
        record_id: str,
        *,
        target_revision: int,
        mutation: MutationContext,
    ) -> MemoryRecordView:
        async with self._sf() as session:
            row = await session.get(MemoryRecordRow, record_id)
            if row is None or row.user_id != user_id:
                raise MemoryNotFoundError(f"memory record {record_id} not found")
            if row.status == "deleted":
                raise MemoryConflictError("deleted memory cannot be rolled back")
            target = await session.get(
                MemoryRecordVersionRow, (record_id, user_id, target_revision)
            )
            if target is None:
                raise MemoryNotFoundError(
                    f"memory revision {record_id}@{target_revision} not found"
                )
            before_revision = int(row.revision)
            before_status = row.status
            row.content = target.content
            row.payload = target.payload
            row.valid_from = target.valid_from
            row.valid_to = target.valid_to
            row.status = "active"
            row.revision = before_revision + 1
            row.updated_at = _now()
            session.add(
                MemoryRecordVersionRow(
                    memory_id=row.id,
                    user_id=user_id,
                    revision=row.revision,
                    status=row.status,
                    content=row.content,
                    payload=row.payload,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                    changed_at=row.updated_at,
                )
            )
            _ledger(
                session,
                user_id=user_id,
                mutation=mutation,
                before_id=row.id,
                after_id=row.id,
                before_revision=before_revision,
                after_revision=row.revision,
                before_status=before_status,
                after_status=row.status,
                op_json={"target_revision": target_revision},
                inverse_json={"content": target.content, "payload": target.payload},
            )
            await _enqueue_outbox(session, row, op="upsert")
            await session.commit()
            return _record_view(row)

    async def last_ledger(
        self, user_id: str, *, kind: str | None = None
    ) -> MemoryLedgerView | None:
        async with self._sf() as session:
            statement = select(MemoryMutationLedgerRow).where(
                MemoryMutationLedgerRow.user_id == user_id
            )
            if kind is not None:
                statement = statement.where(MemoryMutationLedgerRow.op_type == kind)
            row = await session.scalar(
                statement.order_by(MemoryMutationLedgerRow.created_at.desc()).limit(1)
            )
            return _ledger_view(row) if row is not None else None

    async def create_skill_candidate(
        self,
        user_id: str,
        *,
        private_name: str,
        description: str | None,
        trigger_text: str,
        package_snapshot: dict[str, Any],
        package_sha256: str,
        skill_md_sha256: str,
        source_episode_ids: Sequence[str],
        origin_idempotency_key: str,
        validation_report: dict[str, Any] | None = None,
    ) -> SkillCandidateView:
        async with self._sf() as session:
            row = MemorySkillCandidateRow(
                user_id=user_id,
                status="pending_confirmation",
                revision=0,
                private_name=private_name,
                description=description,
                trigger_text=trigger_text,
                package_snapshot=package_snapshot,
                package_sha256=package_sha256,
                skill_md_sha256=skill_md_sha256,
                validation_report=validation_report or {},
                source_episode_ids=list(source_episode_ids),
                origin_idempotency_key=origin_idempotency_key,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(row)
            await session.commit()
            return _candidate_view(row)

    async def get_skill_candidate(
        self, user_id: str, candidate_id: str
    ) -> SkillCandidateView | None:
        async with self._sf() as session:
            row = await session.get(MemorySkillCandidateRow, candidate_id)
            if row is None or row.user_id != user_id:
                return None
            return _candidate_view(row)

    async def list_skill_candidates(
        self, user_id: str, *, status: str | None = None
    ) -> list[SkillCandidateView]:
        async with self._sf() as session:
            statement = select(MemorySkillCandidateRow).where(
                MemorySkillCandidateRow.user_id == user_id
            )
            if status is not None:
                statement = statement.where(MemorySkillCandidateRow.status == status)
            rows = (
                await session.scalars(
                    statement.order_by(MemorySkillCandidateRow.created_at.desc())
                )
            ).all()
            return [_candidate_view(row) for row in rows]

    async def update_skill_candidate(
        self,
        user_id: str,
        candidate_id: str,
        *,
        expected_revision: int,
        changes: dict[str, Any],
    ) -> SkillCandidateView:
        allowed = {"status", "skill_id", "skill_version_id", "validation_report", "rejected_reason"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported skill candidate fields: {sorted(unknown)}")
        async with self._sf() as session:
            row = await session.get(MemorySkillCandidateRow, candidate_id)
            if row is None or row.user_id != user_id:
                raise MemoryNotFoundError(f"skill candidate {candidate_id} not found")
            if int(row.revision) != expected_revision:
                raise MemoryConflictError(
                    f"skill candidate changed (expected revision {expected_revision}, found {row.revision})",
                    current_revision=int(row.revision),
                )
            for key, value in changes.items():
                setattr(row, key, value)
            row.revision = int(row.revision) + 1
            row.updated_at = _now()
            await session.commit()
            return _candidate_view(row)

    async def list_conflicts(
        self, user_id: str, *, include_resolved: bool = False
    ) -> list[MemoryConflictView]:
        async with self._sf() as session:
            statement = select(MemoryConflictRow).where(MemoryConflictRow.user_id == user_id)
            if not include_resolved:
                statement = statement.where(MemoryConflictRow.status == "open")
            rows = (
                await session.scalars(
                    statement.order_by(MemoryConflictRow.created_at.desc())
                )
            ).all()
            return [_conflict_view(row) for row in rows]

    async def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        *,
        expected_revision: int,
        resolution: str,
        merged_content: str | None = None,
    ) -> MemoryConflictView:
        async with self._sf() as session:
            row = await session.get(MemoryConflictRow, conflict_id)
            if row is None or row.user_id != user_id:
                raise MemoryNotFoundError(f"memory conflict {conflict_id} not found")
            if int(row.revision) != expected_revision:
                raise MemoryConflictError(
                    f"memory conflict changed (expected revision {expected_revision}, found {row.revision})",
                    current_revision=int(row.revision),
                )
            row.status = "resolved"
            row.revision = int(row.revision) + 1
            row.resolved_at = _now()
            if merged_content:
                row.candidate_snapshot = {**dict(row.candidate_snapshot or {}), "merged_content": merged_content}
            await session.commit()
            return _conflict_view(row)

    async def clear_user(
        self, user_id: str, *, reason: str = "user_clear"
    ) -> None:
        now = _now()
        async with self._sf() as session:
            scope = await session.get(MemoryScopeMetaRow, user_id)
            if scope is None:
                scope = MemoryScopeMetaRow(user_id=user_id, epoch=0, updated_at=now)
                session.add(scope)
            scope.epoch = int(scope.epoch) + 1
            scope.cleared_at = now
            scope.updated_at = now
            rows = (
                await session.scalars(
                    select(MemoryRecordRow).where(
                        MemoryRecordRow.user_id == user_id,
                        MemoryRecordRow.status.in_(("active", "pending_review", "dormant")),
                    )
                )
            ).all()
            for row in rows:
                row.status = "deleted"
                row.deleted_at = now
                row.revision = int(row.revision) + 1
                row.updated_at = now
                session.add(
                    MemoryRecordVersionRow(
                        memory_id=row.id,
                        user_id=user_id,
                        revision=row.revision,
                        status=row.status,
                        content=row.content,
                        payload=row.payload,
                        valid_from=row.valid_from,
                        valid_to=row.valid_to,
                        changed_at=now,
                    )
                )
                await _enqueue_outbox(session, row, op="delete")
            await session.commit()

    async def write_conflict(
        self,
        user_id: str,
        *,
        kind: str,
        current_snapshot: dict[str, Any],
        candidate_snapshot: dict[str, Any],
        evidence_refs: Sequence[str],
        field_path: str | None = None,
        topic_key: str | None = None,
        current_record_id: str | None = None,
        candidate_record_id: str | None = None,
    ) -> MemoryConflictView:
        async with self._sf() as session:
            row = MemoryConflictRow(
                user_id=user_id,
                kind=kind,
                field_path=field_path,
                topic_key=topic_key,
                current_record_id=current_record_id,
                candidate_record_id=candidate_record_id,
                current_snapshot=current_snapshot,
                candidate_snapshot=candidate_snapshot,
                status="open",
                evidence_refs=list(evidence_refs),
                revision=0,
                created_at=_now(),
            )
            session.add(row)
            await session.commit()
            return _conflict_view(row)

    async def touch_hits(self, user_id: str, record_ids: Sequence[str], *, now: datetime) -> None:
        if not record_ids:
            return
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(MemoryRecordRow).where(
                        MemoryRecordRow.user_id == user_id,
                        MemoryRecordRow.id.in_(record_ids),
                    )
                )
            ).all()
            for row in rows:
                row.hit_count = int(row.hit_count) + 1
                row.last_active_at = now
                row.updated_at = now
                if _projects(row):
                    await _enqueue_outbox(session, row, op="upsert")
            await session.commit()

    # -- sessions / ingest jobs --------------------------------------------

    async def get_open_session(self, user_id: str, thread_id: str) -> MemorySessionView | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(MemoryEpisodeSessionRow).where(
                    MemoryEpisodeSessionRow.user_id == user_id,
                    MemoryEpisodeSessionRow.thread_id == thread_id,
                    MemoryEpisodeSessionRow.status == "open",
                )
            )
            return _session_view(row) if row is not None else None

    async def get_latest_session(
        self, user_id: str, thread_id: str
    ) -> MemorySessionView | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(MemoryEpisodeSessionRow)
                .where(
                    MemoryEpisodeSessionRow.user_id == user_id,
                    MemoryEpisodeSessionRow.thread_id == thread_id,
                )
                .order_by(MemoryEpisodeSessionRow.created_at.desc())
                .limit(1)
            )
            return _session_view(row) if row is not None else None

    async def get_ingest_job_by_id(self, job_id: str) -> MemoryIngestJobView | None:
        async with self._sf() as session:
            row = await session.get(MemoryIngestJobRow, job_id)
            return _job_view(row) if row is not None else None

    async def get_session(self, session_id: str) -> MemorySessionView | None:
        async with self._sf() as session:
            row = await session.get(MemoryEpisodeSessionRow, session_id)
            return _session_view(row) if row is not None else None

    async def get_ingest_job(self, episode_key: str) -> MemoryIngestJobView | None:
        async with self._sf() as session:
            row = await session.scalar(
                select(MemoryIngestJobRow).where(MemoryIngestJobRow.episode_key == episode_key)
            )
            return _job_view(row) if row is not None else None

    async def list_open_sessions(
        self, *, before: datetime | None = None
    ) -> list[MemorySessionView]:
        async with self._sf() as session:
            statement = select(MemoryEpisodeSessionRow).where(
                MemoryEpisodeSessionRow.status == "open"
            )
            if before is not None:
                statement = statement.where(MemoryEpisodeSessionRow.last_activity_at < before)
            rows = (
                await session.scalars(
                    statement.order_by(MemoryEpisodeSessionRow.last_activity_at.asc())
                )
            ).all()
            return [_session_view(row) for row in rows]

    async def count_open_sessions(self, user_id: str, thread_id: str) -> int:
        async with self._sf() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(MemoryEpisodeSessionRow)
                .where(
                    MemoryEpisodeSessionRow.user_id == user_id,
                    MemoryEpisodeSessionRow.thread_id == thread_id,
                    MemoryEpisodeSessionRow.status == "open",
                )
            )
            return int(value or 0)

    async def open_session(
        self, user_id: str, thread_id: str, *, run_id: str, turn_id: str, now: datetime
    ) -> MemorySessionView:
        async with self._sf() as session:
            row = MemoryEpisodeSessionRow(
                user_id=user_id,
                thread_id=thread_id,
                status="open",
                open_turn_id=turn_id,
                last_turn_id=turn_id,
                turn_refs=[],
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryEpisodeSessionRow).where(
                        MemoryEpisodeSessionRow.user_id == user_id,
                        MemoryEpisodeSessionRow.thread_id == thread_id,
                        MemoryEpisodeSessionRow.status == "open",
                    )
                )
                if existing is None:
                    raise
                return _session_view(existing)
            return _session_view(row)

    async def append_session_refs(
        self, session_id: str, refs: Sequence[dict[str, Any]], *, now: datetime
    ) -> MemorySessionView:
        async with self._sf() as session:
            row = await session.get(MemoryEpisodeSessionRow, session_id)
            if row is None:
                raise MemoryNotFoundError(f"episode session {session_id} not found")
            current = list(row.turn_refs or [])
            seen = {str(item.get("message_id")) for item in current if isinstance(item, dict)}
            for ref in refs:
                message_id = str(ref.get("message_id") or "")
                if message_id and message_id in seen:
                    continue
                current.append(dict(ref))
                if message_id:
                    seen.add(message_id)
            row.turn_refs = current
            if refs:
                row.last_turn_id = str(refs[-1].get("message_id") or row.last_turn_id or "")
            row.last_activity_at = now
            row.updated_at = now
            await session.commit()
            return _session_view(row)

    async def close_session(
        self, session_id: str, *, reason: str, episode_key: str, now: datetime
    ) -> MemorySessionView:
        async with self._sf() as session:
            row = await session.get(MemoryEpisodeSessionRow, session_id)
            if row is None:
                raise MemoryNotFoundError(f"episode session {session_id} not found")
            row.status = "closed"
            row.close_reason = reason
            row.episode_key = episode_key
            row.closed_at = now
            row.updated_at = now
            await session.commit()
            return _session_view(row)

    async def enqueue_ingest_job(
        self, session_id: str, *, episode_key: str, payload: dict[str, Any]
    ) -> MemoryIngestJobView:
        async with self._sf() as session:
            existing = await session.scalar(
                select(MemoryIngestJobRow).where(MemoryIngestJobRow.episode_key == episode_key)
            )
            if existing is not None:
                return _job_view(existing)
            session_row = await session.get(MemoryEpisodeSessionRow, session_id)
            if session_row is None:
                raise MemoryNotFoundError(f"episode session {session_id} not found")
            row = MemoryIngestJobRow(
                user_id=session_row.user_id,
                thread_id=session_row.thread_id,
                episode_session_id=session_id,
                episode_key=episode_key,
                job_type="episodic_dehydrate",
                status="pending",
                attempts=0,
                payload=payload,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(MemoryIngestJobRow).where(
                        MemoryIngestJobRow.episode_key == episode_key
                    )
                )
                if existing is None:
                    raise
                return _job_view(existing)
            return _job_view(row)

    async def claim_ingest_job(
        self, *, worker_id: str, lease_seconds: int = 60
    ) -> MemoryIngestJobView | None:
        now = _now()
        async with self._sf() as session:
            row = await session.scalar(
                select(MemoryIngestJobRow)
                .where(MemoryIngestJobRow.status.in_(["pending", "error"]))
                .order_by(MemoryIngestJobRow.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.status = "locked"
            row.lease_until = now + _seconds(lease_seconds)
            row.attempts = int(row.attempts) + 1
            row.updated_at = now
            await session.commit()
            return _job_view(row)

    async def finish_ingest_job(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._sf() as session:
            row = await session.get(MemoryIngestJobRow, job_id)
            if row is None:
                return
            row.status = status
            row.last_error = error
            if payload is not None:
                row.payload = {**dict(row.payload or {}), **payload}
            row.lease_until = None
            row.updated_at = _now()
            await session.commit()

    # -- outbox -------------------------------------------------------------

    async def list_outbox(self, user_id: str, *, limit: int = 100) -> list[MemoryOutboxView]:
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(MemoryIndexOutboxRow)
                    .where(MemoryIndexOutboxRow.user_id == user_id)
                    .order_by(MemoryIndexOutboxRow.created_at.asc())
                    .limit(limit)
                )
            ).all()
            return [_outbox_view(row) for row in rows]

    async def claim_outbox(self, limit: int = 50) -> list[MemoryOutboxView]:
        now = _now()
        async with self._sf() as session:
            rows = (
                await session.scalars(
                    select(MemoryIndexOutboxRow)
                    .where(
                        MemoryIndexOutboxRow.state == "pending",
                        (MemoryIndexOutboxRow.next_attempt_at.is_(None))
                        | (MemoryIndexOutboxRow.next_attempt_at <= now),
                    )
                    .order_by(MemoryIndexOutboxRow.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                row.state = "locked"
                row.attempts = int(row.attempts) + 1
                row.updated_at = now
            await session.commit()
            return [_outbox_view(row) for row in rows]

    async def finish_outbox(self, outbox_id: str) -> None:
        async with self._sf() as session:
            row = await session.get(MemoryIndexOutboxRow, outbox_id)
            if row is not None:
                row.state = "done"
                row.updated_at = _now()
                await session.commit()

    async def retry_outbox(self, outbox_id: str, *, error: str, delay_seconds: float) -> None:
        async with self._sf() as session:
            row = await session.get(MemoryIndexOutboxRow, outbox_id)
            if row is None:
                return
            row.state = "pending"
            row.last_error = error
            row.next_attempt_at = _now() + _seconds(delay_seconds)
            row.updated_at = _now()
            await session.commit()


def _projects(row: MemoryRecordRow) -> bool:
    return row.status == "active" and row.kind in _PROJECTED_KINDS


async def _enqueue_outbox(session: Any, row: MemoryRecordRow, *, op: str) -> None:
    existing = await session.scalar(
        select(MemoryIndexOutboxRow.id).where(
            MemoryIndexOutboxRow.memory_id == row.id,
            MemoryIndexOutboxRow.revision == int(row.revision),
            MemoryIndexOutboxRow.op == op,
        )
    )
    if existing is not None:
        # A touch or repeated transition for the same revision must not violate
        # the outbox unique key.  The pending event will read current PG state.
        return
    scope = await session.get(MemoryScopeMetaRow, row.user_id)
    epoch = int(scope.epoch) if scope is not None else 0
    session.add(
        MemoryIndexOutboxRow(
            user_id=row.user_id,
            pk=f"{row.user_id}:{row.id}",
            memory_id=row.id,
            op=op,
            revision=int(row.revision),
            epoch=epoch,
            state="pending",
            attempts=0,
            next_attempt_at=None,
            created_at=_now(),
            updated_at=_now(),
        )
    )


def _ledger(
    session: Any,
    *,
    user_id: str,
    mutation: MutationContext,
    before_id: str | None,
    after_id: str | None,
    before_revision: int | None,
    after_revision: int | None,
    before_status: str | None,
    after_status: str | None,
    op_json: dict[str, Any],
    inverse_json: dict[str, Any],
) -> None:
    session.add(
        MemoryMutationLedgerRow(
            user_id=user_id,
            actor_type=mutation.actor_type,
            actor_id=mutation.actor_id,
            op_type=mutation.op_type,
            before_id=before_id,
            after_id=after_id,
            before_revision=before_revision,
            after_revision=after_revision,
            before_status=before_status,
            after_status=after_status,
            op_json=_jsonable(op_json),
            inverse_json=_jsonable(inverse_json),
            source_episode_ids=list(mutation.source_episode_ids),
            reason=mutation.reason,
            created_at=_now(),
        )
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _seconds(value: float) -> Any:
    from datetime import timedelta

    return timedelta(seconds=value)


def _candidate_view(row: MemorySkillCandidateRow) -> SkillCandidateView:
    return SkillCandidateView(
        id=row.id,
        user_id=row.user_id,
        status=row.status,
        revision=int(row.revision),
        private_name=row.private_name,
        description=row.description,
        trigger_text=row.trigger_text,
        package_snapshot=dict(row.package_snapshot or {}),
        package_sha256=row.package_sha256,
        skill_md_sha256=row.skill_md_sha256,
        source_episode_ids=list(row.source_episode_ids or []),
        origin_idempotency_key=row.origin_idempotency_key,
        skill_id=row.skill_id,
        skill_version_id=row.skill_version_id,
        validation_report=dict(row.validation_report or {}),
        created_at=row.created_at,
    )


def _conflict_view(row: MemoryConflictRow) -> MemoryConflictView:
    return MemoryConflictView(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        field_path=row.field_path,
        topic_key=row.topic_key,
        current_record_id=row.current_record_id,
        candidate_record_id=row.candidate_record_id,
        current_snapshot=dict(row.current_snapshot or {}),
        candidate_snapshot=dict(row.candidate_snapshot or {}),
        status=row.status,
        revision=int(row.revision),
        evidence_refs=list(row.evidence_refs or []),
        created_at=row.created_at,
    )


def _profile_view(row: MemoryProfileRow) -> MemoryProfileView:
    return MemoryProfileView(
        user_id=row.user_id,
        schema_version=int(row.schema_version),
        revision=int(row.revision),
        profile=dict(row.profile or {}),
        tombstones=dict(row.tombstones or {}),
        updated_at=row.updated_at,
    )


def _record_view(row: MemoryRecordRow) -> MemoryRecordView:
    return MemoryRecordView(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        subtype=row.subtype,
        status=row.status,
        revision=int(row.revision),
        content=row.content,
        payload=dict(row.payload or {}),
        topic_key=row.topic_key,
        scope_condition=row.scope_condition,
        source=row.source,
        evidence_tier=row.evidence_tier,
        confidence=float(row.confidence),
        consolidation_status=row.consolidation_status,
        consolidated_at=row.consolidated_at,
        salience=float(row.salience),
        hit_count=int(row.hit_count),
        last_active_at=row.last_active_at,
        explicit_remember=bool(row.explicit_remember),
        parent_memory_id=row.parent_memory_id,
        correction_of=row.correction_of,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        occurred_at=row.occurred_at,
        thread_id=row.thread_id,
        agent_name=row.agent_name,
        provenance=dict(row.provenance or {}),
        source_episode_ids=list(row.source_episode_ids or []),
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_view(row: MemoryEpisodeSessionRow) -> MemorySessionView:
    return MemorySessionView(
        id=row.id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        status=row.status,
        turn_refs=list(row.turn_refs or []),
        episode_key=row.episode_key,
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _job_view(row: MemoryIngestJobRow) -> MemoryIngestJobView:
    return MemoryIngestJobView(
        id=row.id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        episode_session_id=row.episode_session_id,
        episode_key=row.episode_key,
        status=row.status,
        attempts=int(row.attempts),
        payload=dict(row.payload or {}),
        lease_until=row.lease_until,
    )


def _outbox_view(row: MemoryIndexOutboxRow) -> MemoryOutboxView:
    return MemoryOutboxView(
        id=row.id,
        user_id=row.user_id,
        pk=row.pk,
        memory_id=row.memory_id,
        op=row.op,
        revision=int(row.revision),
        epoch=int(row.epoch),
        state=row.state,
        attempts=int(row.attempts),
    )


def _ledger_view(row: MemoryMutationLedgerRow) -> MemoryLedgerView:
    return MemoryLedgerView(
        tx_id=row.tx_id,
        user_id=row.user_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        op_type=row.op_type,
        before_id=row.before_id,
        after_id=row.after_id,
        before_revision=row.before_revision,
        after_revision=row.after_revision,
        before_status=row.before_status,
        after_status=row.after_status,
        op_json=dict(row.op_json or {}),
        inverse_json=dict(row.inverse_json or {}),
        reason=row.reason,
        created_at=row.created_at,
    )
