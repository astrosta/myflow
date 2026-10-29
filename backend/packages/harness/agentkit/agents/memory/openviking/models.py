"""PostgreSQL tables owned by the OpenViking memory adapter.

These tables do not store OpenViking memory bodies. They only hold the
profile document, session/capture lifecycle, suggestions, onboarding state and
message-level dedup bookkeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryProfileDocumentRow(Base):
    __tablename__ = "memory_profile_documents"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class MemorySyncStateRow(Base):
    __tablename__ = "memory_sync_state"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ov_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ov_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_incarnation: Mapped[str] = mapped_column(String(64), nullable=False)
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stream_shard: Mapped[int] = mapped_column(Integer, nullable=False)
    session_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stream_id: Mapped[str | None] = mapped_column(String(64))
    last_message_id: Mapped[str | None] = mapped_column(String(128))
    last_append_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_auto_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_commit_task_id: Mapped[str | None] = mapped_column(String(128))
    last_archive_uri: Mapped[str | None] = mapped_column(Text)
    last_commit_reason: Mapped[str | None] = mapped_column(String(128))
    last_turn_end_id: Mapped[str | None] = mapped_column(String(128))
    last_turn_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_cursor: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class MemoryProfileOnboardingRow(Base):
    __tablename__ = "memory_profile_onboarding"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_started")
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class MemoryProfileSuggestionRow(Base):
    __tablename__ = "memory_profile_suggestions"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    suggestion_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    field_path: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_uri: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    profile_etag: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_suggestions_user_status_expiry", "user_id", "status", "expires_at"),
        Index("ix_suggestions_user_profile_etag", "user_id", "profile_etag", "status"),
        Index("ix_suggestions_user_field_hash", "user_id", "field_path", "value_hash"),
        Index(
            "uq_pending_suggestion",
            "user_id",
            "field_path",
            "value_hash",
            "profile_etag",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )


class MemoryCaptureDedupRow(Base):
    __tablename__ = "memory_capture_dedup"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_epoch: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_incarnation: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    appending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ov_message_ids: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    appended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_capture_dedup_state_created", "state", "created_at"),
        CheckConstraint(
            "state in ('pending', 'appending', 'appended')",
            name="ck_capture_dedup_state",
        ),
        CheckConstraint(
            "("
            "state = 'pending' and lease_until is null and appended_at is null"
            ") or ("
            "state = 'appending' and lease_until is not null and appended_at is null"
            ") or ("
            "state = 'appended' and appended_at is not null"
            ")",
            name="ck_capture_dedup_state_fields",
        ),
    )


__all__ = [
    "MemoryCaptureDedupRow",
    "MemoryProfileDocumentRow",
    "MemoryProfileOnboardingRow",
    "MemoryProfileSuggestionRow",
    "MemorySyncStateRow",
]
