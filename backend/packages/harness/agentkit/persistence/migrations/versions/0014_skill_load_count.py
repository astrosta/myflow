"""Persist private-skill load counts for Redis seeding and DB fallback.

Revision ID: 0014_skill_load_count
Revises: 0013_skill_enabled
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_skill_load_count"
down_revision = "0013_skill_enabled"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(column["name"] == name for column in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("skills", "load_count"):
        op.add_column(
            "skills",
            sa.Column(
                "load_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if _has_column("skills", "load_count"):
        with op.batch_alter_table("skills") as batch:
            batch.drop_column("load_count")
