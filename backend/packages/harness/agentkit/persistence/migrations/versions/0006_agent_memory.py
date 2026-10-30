"""Historical canonical memory migration (retired).

Revision ID: 0006_agent_memory
Revises: 0005_skill_catalog

The canonical memory runtime and tables were retired in 0009. This revision
remains in the Alembic chain so existing databases can advance to the OpenViking
schema, but it no longer creates legacy tables.
"""

from __future__ import annotations

revision = "0006_agent_memory"
down_revision = "0005_skill_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    return


def downgrade() -> None:
    return
