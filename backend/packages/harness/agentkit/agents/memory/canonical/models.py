"""Canonical memory tables.

PostgreSQL is authoritative; SQLite is the local/test backend.  JSONB is used
on PostgreSQL and falls back to JSON on SQLite.  No table here stores raw
transcripts or full tool output; payload/provenance columns carry bounded
summaries and evidence references only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.persistence.base import Base

JsonB = JSON().with_variant(JSONB, "postgresql")


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryProfileRow(Base):
    __tablename__ = "memory_profiles"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    profile: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    tombstones: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class MemoryRecordRow(Base):
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    topic_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope_condition: Mapped[str] = mapped_column(String(255), nullable=False, default="global")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="conversation")
    evidence_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    consolidation_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    explicit_remember: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correction_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    source_episode_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_memory_records_user_id"),
        Index("ix_memory_records_user_kind_status", "user_id", "kind", "status"),
        Index("ix_memory_records_user_topic_status", "user_id", "topic_key", "status"),
        Index("ix_memory_records_user_task", "user_id", "task_id"),
        Index(
            "ix_memory_records_episodic_pending",
            "user_id",
            "consolidation_status",
            sqlite_where=text("kind = 'episodic'"),
            postgresql_where=text("kind = 'episodic'"),
        ),
        Index(
            "uq_memory_records_active_correction",
            "user_id",
            "correction_of",
            unique=True,
            sqlite_where=text("status = 'active' AND correction_of IS NOT NULL"),
            postgresql_where=text("status = 'active' AND correction_of IS NOT NULL"),
        ),
        Index("ix_memory_records_user_valid_to_kind", "user_id", "valid_to", "kind"),
        Index("ix_memory_records_thread_time", "user_id", "thread_id", "occurred_at"),
    )


class MemoryRecordVersionRow(Base):
    __tablename__ = "memory_record_versions"

    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class MemoryEpisodeSessionRow(Base):
    __tablename__ = "memory_episode_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    task_goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    task_goal_source: Mapped[str] = mapped_column(String(16), nullable=False, default="fallback")
    boundary_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    boundary_uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    open_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    episode_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    turn_refs: Mapped[list[dict[str, Any]]] = mapped_column(JsonB, nullable=False, default=list)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_memory_episode_open",
            "user_id",
            "thread_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_memory_episode_sessions_user_status", "user_id", "status"),
        Index("ix_memory_episode_sessions_user_task", "user_id", "task_id"),
    )


class MemoryIngestJobRow(Base):
    __tablename__ = "memory_ingest_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    episode_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    episode_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, default="episodic_dehydrate")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (Index("ix_memory_ingest_jobs_status_lease", "status", "lease_until"),)


class MemoryConsolidationJobRow(Base):
    __tablename__ = "memory_consolidation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_episode_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    kind_target: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (Index("ix_memory_consolidation_status_lease", "status", "lease_until"),)


class MemorySkillCandidateRow(Base):
    __tablename__ = "memory_skill_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    private_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_text: Mapped[str] = mapped_column(Text, nullable=False)
    package_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_md_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_skill_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_patch: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    source_episode_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    origin_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (Index("ix_memory_skill_candidates_user_status", "user_id", "status"),)


class MemoryConflictRow(Base):
    __tablename__ = "memory_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    field_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    candidate_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    candidate_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    evidence_refs: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_memory_conflicts_user_status", "user_id", "status"),)


class MemoryMutationLedgerRow(Base):
    __tablename__ = "memory_mutation_ledger"

    tx_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    op_type: Mapped[str] = mapped_column(String(32), nullable=False)
    before_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    before_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    op_json: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    inverse_json: Mapped[dict[str, Any]] = mapped_column(JsonB, nullable=False, default=dict)
    rollback_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_episode_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (Index("ix_memory_ledger_user_created", "user_id", "created_at"),)


class MemoryIndexOutboxRow(Base):
    __tablename__ = "memory_index_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pk: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(36), nullable=False)
    op: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "memory_id", "revision", "op", name="uq_memory_index_outbox_revision"
        ),
        Index("ix_memory_index_outbox_state_next", "state", "next_attempt_at"),
    )


class MemoryScopeMetaRow(Base):
    __tablename__ = "memory_scope_meta"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class MemoryProjectionStateRow(Base):
    __tablename__ = "memory_projection_state"

    collection: Mapped[str] = mapped_column(String(255), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


__all__ = [
    "MemoryConflictRow",
    "MemoryConsolidationJobRow",
    "MemoryEpisodeSessionRow",
    "MemoryIndexOutboxRow",
    "MemoryIngestJobRow",
    "MemoryMutationLedgerRow",
    "MemoryProfileRow",
    "MemoryProjectionStateRow",
    "MemoryRecordRow",
    "MemoryRecordVersionRow",
    "MemoryScopeMetaRow",
    "MemorySkillCandidateRow",
]
