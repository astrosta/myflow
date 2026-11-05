"""ORM tables for the user-isolated Git-backed skill catalog.

PostgreSQL is authoritative in production; SQLite is the test backend.  Partial
unique indexes therefore carry both sqlite_where and postgresql_where so the
database enforces the same invariants in both places.

The public-release/review tables were removed in v2.  Skills are private to
their owner; a saved version becomes runtime-visible only through the explicit
scan -> activate flow.
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
    """User-private skill identity. The name may change; id is the stable id."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    private_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 用户开关；只有 active 技能可以切换，关闭后不进入提示词/检索/投影。
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: `load_skills` 成功加载次数；Redis ZSET 的持久化来源，内置技能不入库。
    load_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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
    )


class SkillVersionRow(Base):
    """Local immutable version; Git coordinates are attached on activation."""

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
        String(16),
        nullable=False,
        default="unindexed",
        server_default="unindexed",
        index=True,
    )
    #: 对账快路径的退避状态：坏行不能每轮都吃满修复预算。
    index_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    index_last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    index_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class SkillPrivateActiveRow(Base):
    """The one active version for a skill. Written only by explicit activation."""

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


class SkillSecurityScanRow(Base):
    """Immutable result of one cisco-ai-skill-scanner run against a version."""

    __tablename__ = "skill_security_scans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.id"), nullable=False, index=True
    )
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_versions.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="completed", server_default="completed"
    )
    is_safe: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default=text("false")
    )
    max_severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SAFE", server_default="SAFE"
    )
    findings_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    findings_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scanner: Mapped[str] = mapped_column(
        String(64), nullable=False, default="cisco-ai-skill-scanner"
    )
    scanner_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        Index("ix_skill_security_scans_skill_created", "skill_id", "created_at"),
        Index("ix_skill_security_scans_version_created", "version_id", "created_at"),
    )


class SkillAuditEventRow(Base):
    """Append-only lifecycle audit. Never stores private body content."""

    __tablename__ = "skill_audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    "SkillRow",
    "SkillSecurityScanRow",
    "SkillVersionRow",
]
