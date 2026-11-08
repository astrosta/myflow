"""Add the authoritative ContextForge tool catalog table.

Revision ID: 0017_contextforge_tools
Revises: 0016_drop_profile_suggestions

This revision is re-entrant like every other DDL revision in this chain: an
empty database is built by Base.metadata.create_all and stamped at head, so the
table may already exist when upgrade() runs.  There is no historical data to
backfill - ContextForge has not shipped - so the upgrade only creates the
current shape.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.contextforge.store.model  # noqa: F401 - register metadata
from agentkit.persistence.base import Base

revision = "0017_contextforge_tools"
down_revision = "0016_drop_profile_suggestions"
branch_labels = None
depends_on = None

_TABLE = "contextforge_tools"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    Base.metadata.tables[_TABLE].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    if _has_table(_TABLE):
        Base.metadata.tables[_TABLE].drop(op.get_bind())


__all__ = ["revision", "down_revision", "upgrade", "downgrade"]
