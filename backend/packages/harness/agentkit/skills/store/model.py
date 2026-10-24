"""Twelve ORM tables for the Git-backed skill catalog.

PostgreSQL is authoritative in production; SQLite is the test backend.  Partial
unique indexes therefore carry both `sqlite_where` and `postgresql_where` so the
database enforces the same invariants in both places.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.persistence.base import Base


def _uuid() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class SkillRow(Base):
    """Skill identity. The name may change; `id` is the stable API id."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    private_name: Mapped[str] = mapped_column(String(128), nullable=False)
    public_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_skills_owner_private_name_active",
            "owner_user_id",
            "private_name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_skills_public_name",
            "public_name",
            unique=True,
            sqlite_where=text("public_name IS NOT NULL"),
            postgresql_where=text("public_name IS NOT NULL"),
        ),
    )


class SkillVersionRow(Base):
    """Immutable package version; only `index_status` may change after insert."""

    __tablename__ = "skill_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), nullable=False, index=True
    )
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False)
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_md_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    draft_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    draft_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    skill_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    index_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("skill_id", "skill_version", name="uq_skill_versions_skill_version"),
        UniqueConstraint(
            "skill_id", "package_sha256", name="uq_skill_versions_skill_package_sha256"
        ),
    )


class SkillSubmissionRow(Base):
    """One review submission around an immutable version commit."""

    __tablename__ = "skill_submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), nullable=False, index=True
    )
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_versions.id"), nullable=False, index=True
    )
    submitted_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_release_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_skill_submissions_skill_pending",
            "skill_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_skill_submissions_status_submitted", "status", "submitted_at"),
    )


class SkillReleaseBatchRow(Base):
    """One public approval commit; batches allow future multi-skill approvals."""

    __tablename__ = "skill_release_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    git_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    git_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    stats_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_skill_release_batches_git_commit", "git_commit"),)


class SkillReleaseRow(Base):
    """Immutable public release row; the active pointer selects the current one."""

    __tablename__ = "skill_releases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_release_batches.id"), nullable=False
    )
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_versions.id"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), nullable=False, index=True
    )
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False)
    git_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    git_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    skill_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    public_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("batch_id", "skill_id", name="uq_skill_releases_batch_skill"),
        Index("ix_skill_releases_skill_created", "skill_id", "created_at"),
        Index("ix_skill_releases_git_commit", "git_commit"),
        Index("ix_skill_releases_skill_version", "skill_version"),
    )


class SkillPublicActiveRow(Base):
    """Current public release pointer. No row means the skill is not public."""

    __tablename__ = "skill_public_active"

    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), primary_key=True
    )
    release_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_releases.id"), nullable=False
    )
    activated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class SkillPrivateActiveRow(Base):
    """Current private version pointer for one owner."""

    __tablename__ = "skill_private_active"

    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), primary_key=True
    )
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_versions.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class SkillSettingsRow(Base):
    """Global public-skill switch. Private skills ignore it."""

    __tablename__ = "skill_settings"

    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class SkillAuditEventRow(Base):
    """Append-only lifecycle audit. Never stores private body content."""

    __tablename__ = "skill_audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    submission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (Index("ix_skill_audit_events_skill_created", "skill_id", "created_at"),)


class SkillDraftFileRow(Base):
    """Unsaved editor staging; never a version and never projected by itself."""

    __tablename__ = "skill_draft_files"

    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text", server_default="text"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class SkillCatalogEpochRow(Base):
    """Singleton catalog generation. Every mutation bumps it; every worker polls it."""

    __tablename__ = "skill_catalog_epoch"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class SkillIndexOutboxRow(Base):
    """Reliable Milvus active-projection queue written in the business transaction."""

    __tablename__ = "skill_index_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    op: Mapped[str] = mapped_column(String(16), nullable=False)
    projection_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        Index("ix_skill_index_outbox_status_next", "status", "next_attempt_at"),
    )


__all__ = [
    "SkillAuditEventRow",
    "SkillCatalogEpochRow",
    "SkillDraftFileRow",
    "SkillIndexOutboxRow",
    "SkillPrivateActiveRow",
    "SkillPublicActiveRow",
    "SkillReleaseBatchRow",
    "SkillReleaseRow",
    "SkillRow",
    "SkillSettingsRow",
    "SkillSubmissionRow",
    "SkillVersionRow",
]
