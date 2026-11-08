"""Add the OpenViking memory adapter tables.

Revision ID: 0008_openviking_memory
Revises: 0007_task_classifier

The migration is re-entrant: create_all/bootstrap may already have created
these tables. It intentionally leaves the legacy canonical memory tables in
place; their removal is a later cutover migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.agents.memory.openviking.models  # noqa: F401 - register metadata
from agentkit.persistence.base import Base

revision = "0008_openviking_memory"
down_revision = "0007_task_classifier"
branch_labels = None
depends_on = None

_TABLES = (
    "memory_profile_documents",
    "memory_sync_state",
    "memory_profile_onboarding",
    "memory_capture_dedup",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if _has_table(name):
            Base.metadata.tables[name].drop(bind, checkfirst=True)
