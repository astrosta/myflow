"""Add skill provenance for manually authored and session-extracted skills.

Revision ID: 0012_skill_source
Revises: 0011_user_isolated_skills

Existing skills are manual. OpenViking session-skill imports set source to
"extracted" and keep the generated skill URI for idempotent re-import.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_skill_source"
down_revision = "0011_user_isolated_skills"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(column["name"] == name for column in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("skills", "source"):
        op.add_column(
            "skills",
            sa.Column(
                "source",
                sa.String(length=16),
                nullable=False,
                server_default="manual",
            ),
        )
    if not _has_column("skills", "source_uri"):
        op.add_column("skills", sa.Column("source_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    for column in ("source_uri", "source"):
        if _has_column("skills", column):
            with op.batch_alter_table("skills") as batch:
                batch.drop_column(column)
