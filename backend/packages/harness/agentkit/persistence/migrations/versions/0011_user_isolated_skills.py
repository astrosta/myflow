"""User-isolated skills: add security scans, drop public review/release state.

Revision ID: 0011_user_isolated_skills
Revises: 0010_users_email_optional

The new model only keeps user-private skills.  Existing skill/version/draft/
active/outbox/audit data is preserved.  Public-release and review tables are
removed because no code reads or writes them anymore; their history is not
silently re-interpreted as private data.

The revision is re-entrant like 0005: a fresh database is created from
Base.metadata and stamped, so every DDL action is guarded by an inspection.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_user_isolated_skills"
down_revision = "0010_users_email_optional"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == name for column in inspector.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == name for index in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_table("skill_security_scans"):
        op.create_table(
            "skill_security_scans",
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
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column("package_sha256", sa.String(length=64), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="completed"
            ),
            sa.Column(
                "is_safe", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "max_severity", sa.String(length=16), nullable=False, server_default="SAFE"
            ),
            sa.Column(
                "findings_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("findings_json", sa.JSON(), nullable=False),
            sa.Column(
                "scanner",
                sa.String(length=64),
                nullable=False,
                server_default="cisco-ai-skill-scanner",
            ),
            sa.Column("scanner_version", sa.String(length=64), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if _has_table("skill_security_scans"):
        for name, columns in (
            ("ix_skill_security_scans_skill_id", ["skill_id"]),
            ("ix_skill_security_scans_version_id", ["version_id"]),
            ("ix_skill_security_scans_owner_user_id", ["owner_user_id"]),
            ("ix_skill_security_scans_skill_created", ["skill_id", "created_at"]),
            ("ix_skill_security_scans_version_created", ["version_id", "created_at"]),
        ):
            if not _has_index("skill_security_scans", name):
                op.create_index(name, "skill_security_scans", columns)

    # Drop public/review tables child-first.  No surviving foreign key points at
    # them after the code removal above.
    for table in (
        "skill_settings",
        "skill_public_active",
        "skill_releases",
        "skill_release_batches",
        "skill_submissions",
    ):
        if _has_table(table):
            op.drop_table(table)

    if _has_table("skills"):
        if _has_index("skills", "uq_skills_public_name"):
            op.drop_index("uq_skills_public_name", table_name="skills")
        if _has_column("skills", "public_name"):
            # SQLite cannot rebuild the referenced "skills" table with foreign
            # keys enabled while surviving child rows exist.  The column is no
            # longer mapped or written, so keeping it as a harmless legacy
            # nullable column is safer than a destructive table rebuild.
            if op.get_bind().dialect.name != "sqlite":
                op.drop_column("skills", "public_name")


def downgrade() -> None:
    """Best-effort downgrade; dropped public history is not recoverable."""
    if _has_table("skills") and not _has_column("skills", "public_name"):
        with op.batch_alter_table("skills") as batch:
            batch.add_column(sa.Column("public_name", sa.String(length=128), nullable=True))
    if _has_table("skill_security_scans"):
        op.drop_table("skill_security_scans")
