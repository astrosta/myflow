"""Persist the private-skill enable switch.

Revision ID: 0013_skill_enabled
Revises: 0012_skill_source

The switch replaces the old skills_prefs.json file for catalog-backed private
skills. Existing skills stay enabled by default.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_skill_enabled"
down_revision = "0012_skill_source"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(column["name"] == name for column in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("skills", "enabled"):
        op.add_column(
            "skills",
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    if _has_column("skills", "enabled"):
        with op.batch_alter_table("skills") as batch:
            batch.drop_column("enabled")
