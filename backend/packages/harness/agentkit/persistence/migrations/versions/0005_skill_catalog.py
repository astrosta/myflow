"""Add the twelve skill catalog tables.

Revision ID: 0005_skill_catalog
Revises: 0004_knowledge_base

The revision is intentionally re-entrant: an empty database is created by
`Base.metadata.create_all` and then stamped at head, so the tables may already
exist when this upgrade runs.  Match 0004 and inspect before every DDL action.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import TextClause

revision = "0005_skill_catalog"
down_revision = "0004_knowledge_base"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table: str, name: str) -> bool:
    return any(index["name"] == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def _has_columns(table: str, columns: list[str]) -> bool:
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)
    }
    return all(column in existing for column in columns)


def _create_index(
    name: str,
    table: str,
    columns: list[str],
    *,
    unique: bool = False,
    sqlite_where: TextClause | None = None,
    postgresql_where: TextClause | None = None,
) -> None:
    if _has_index(table, name):
        return
    # The database may already be at a newer shape (for example after
    # Base.metadata.create_all and a stamped baseline).  Historical migrations
    # must not create an index for a column the current shape has removed.
    if not _has_columns(table, columns):
        return
    kwargs: dict[str, object] = {"unique": unique}
    if sqlite_where is not None:
        kwargs["sqlite_where"] = sqlite_where
    if postgresql_where is not None:
        kwargs["postgresql_where"] = postgresql_where
    op.create_index(name, table, columns, **kwargs)


def upgrade() -> None:
    if not _has_table("skills"):
        op.create_table(
            "skills",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column("private_name", sa.String(length=128), nullable=False),
            sa.Column("public_name", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"])
    _create_index(
        "uq_skills_owner_private_name_active",
        "skills",
        ["owner_user_id", "private_name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    _create_index(
        "uq_skills_public_name",
        "skills",
        ["public_name"],
        unique=True,
        sqlite_where=sa.text("public_name IS NOT NULL"),
        postgresql_where=sa.text("public_name IS NOT NULL"),
    )

    if not _has_table("skill_versions"):
        op.create_table(
            "skill_versions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                nullable=False,
            ),
            sa.Column("skill_version", sa.String(length=32), nullable=False),
            sa.Column("version_seq", sa.Integer(), nullable=False),
            sa.Column("package_sha256", sa.String(length=64), nullable=False),
            sa.Column("skill_md_sha256", sa.String(length=64), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("draft_ref", sa.String(length=255), nullable=True),
            sa.Column("draft_commit", sa.String(length=40), nullable=True),
            sa.Column("skill_path", sa.String(length=1024), nullable=False),
            sa.Column(
                "index_status", sa.String(length=16), nullable=False, server_default="pending"
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "skill_id", "skill_version", name="uq_skill_versions_skill_version"
            ),
            sa.UniqueConstraint(
                "skill_id",
                "package_sha256",
                name="uq_skill_versions_skill_package_sha256",
            ),
        )
    _create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])

    if not _has_table("skill_submissions"):
        op.create_table(
            "skill_submissions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                nullable=False,
            ),
            sa.Column(
                "version_id",
                sa.String(length=64),
                sa.ForeignKey("skill_versions.id"),
                nullable=False,
            ),
            sa.Column("submitted_commit", sa.String(length=40), nullable=False),
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reviewed_by", sa.String(length=64), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column("public_release_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index("ix_skill_submissions_skill_id", "skill_submissions", ["skill_id"])
    _create_index("ix_skill_submissions_version_id", "skill_submissions", ["version_id"])
    _create_index("ix_skill_submissions_owner_user_id", "skill_submissions", ["owner_user_id"])
    _create_index(
        "uq_skill_submissions_skill_pending",
        "skill_submissions",
        ["skill_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    _create_index(
        "ix_skill_submissions_status_submitted",
        "skill_submissions",
        ["status", "submitted_at"],
    )

    if not _has_table("skill_release_batches"):
        op.create_table(
            "skill_release_batches",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("git_repository", sa.String(length=512), nullable=False),
            sa.Column("git_commit", sa.String(length=40), nullable=False),
            sa.Column("approved_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("stats_json", sa.JSON(), nullable=False),
        )
    _create_index(
        "ix_skill_release_batches_git_commit", "skill_release_batches", ["git_commit"]
    )

    if not _has_table("skill_releases"):
        op.create_table(
            "skill_releases",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "batch_id",
                sa.String(length=64),
                sa.ForeignKey("skill_release_batches.id"),
                nullable=False,
            ),
            sa.Column(
                "version_id",
                sa.String(length=64),
                sa.ForeignKey("skill_versions.id"),
                nullable=False,
            ),
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                nullable=False,
            ),
            sa.Column("skill_version", sa.String(length=32), nullable=False),
            sa.Column("git_repository", sa.String(length=512), nullable=False),
            sa.Column("git_commit", sa.String(length=40), nullable=False),
            sa.Column("skill_path", sa.String(length=1024), nullable=False),
            sa.Column("public_name", sa.String(length=128), nullable=False),
            sa.Column("source_commit", sa.String(length=40), nullable=True),
            sa.Column("approved_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("batch_id", "skill_id", name="uq_skill_releases_batch_skill"),
        )
    _create_index("ix_skill_releases_skill_id", "skill_releases", ["skill_id"])
    _create_index(
        "ix_skill_releases_skill_created", "skill_releases", ["skill_id", "created_at"]
    )
    _create_index("ix_skill_releases_git_commit", "skill_releases", ["git_commit"])
    _create_index("ix_skill_releases_skill_version", "skill_releases", ["skill_version"])

    if not _has_table("skill_public_active"):
        op.create_table(
            "skill_public_active",
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                primary_key=True,
            ),
            sa.Column(
                "release_id",
                sa.String(length=64),
                sa.ForeignKey("skill_releases.id"),
                nullable=False,
            ),
            sa.Column("activated_by", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("skill_private_active"):
        op.create_table(
            "skill_private_active",
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                primary_key=True,
            ),
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column(
                "version_id",
                sa.String(length=64),
                sa.ForeignKey("skill_versions.id"),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index(
        "ix_skill_private_active_owner_user_id", "skill_private_active", ["owner_user_id"]
    )

    if not _has_table("skill_settings"):
        op.create_table(
            "skill_settings",
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id"),
                primary_key=True,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_by", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("skill_audit_events"):
        op.create_table(
            "skill_audit_events",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("actor", sa.String(length=64), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("skill_id", sa.String(length=64), nullable=True),
            sa.Column("submission_id", sa.String(length=64), nullable=True),
            sa.Column("release_id", sa.String(length=64), nullable=True),
            sa.Column("detail_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index("ix_skill_audit_events_actor", "skill_audit_events", ["actor"])
    _create_index("ix_skill_audit_events_action", "skill_audit_events", ["action"])
    _create_index("ix_skill_audit_events_skill_id", "skill_audit_events", ["skill_id"])
    _create_index(
        "ix_skill_audit_events_skill_created",
        "skill_audit_events",
        ["skill_id", "created_at"],
    )

    if not _has_table("skill_draft_files"):
        op.create_table(
            "skill_draft_files",
            sa.Column(
                "skill_id",
                sa.String(length=64),
                sa.ForeignKey("skills.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("path", sa.String(length=1024), primary_key=True),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="text"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("skill_catalog_epoch"):
        op.create_table(
            "skill_catalog_epoch",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("skill_index_outbox"):
        op.create_table(
            "skill_index_outbox",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("op", sa.String(length=16), nullable=False),
            sa.Column("projection_key", sa.String(length=255), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("owner_user_id", sa.String(length=64), nullable=True),
            sa.Column("skill_id", sa.String(length=64), nullable=False),
            sa.Column("version_id", sa.String(length=64), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index("ix_skill_index_outbox_projection_key", "skill_index_outbox", ["projection_key"])
    _create_index(
        "ix_skill_index_outbox_status_next",
        "skill_index_outbox",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    for table in (
        "skill_catalog_epoch",
        "skill_index_outbox",
        "skill_draft_files",
        "skill_audit_events",
        "skill_settings",
        "skill_private_active",
        "skill_public_active",
        "skill_releases",
        "skill_release_batches",
        "skill_submissions",
        "skill_versions",
        "skills",
    ):
        if _has_table(table):
            op.drop_table(table)
