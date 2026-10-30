"""Drop the legacy canonical memory tables.

Revision ID: 0009_drop_canonical_memory
Revises: 0008_openviking_memory

The OpenViking adapter (0008) is the long-term memory authority after this
revision. The canonical tables are empty in the supported cutover; this
migration intentionally does not copy or transform rows. Downgrade recreates
the schema for local recovery only, not the data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.agents.memory.canonical.models  # noqa: F401 - downgrade metadata
from agentkit.persistence.base import Base

revision = "0009_drop_canonical_memory"
down_revision = "0008_openviking_memory"
branch_labels = None
depends_on = None

# Reverse dependency order; the canonical schema has no FK constraints today,
# but dropping leaves in this order keeps the migration safe if one is added.
_TABLES = (
    "memory_index_outbox",
    "memory_projection_state",
    "memory_scope_meta",
    "memory_mutation_ledger",
    "memory_conflicts",
    "memory_skill_candidates",
    "memory_consolidation_jobs",
    "memory_ingest_jobs",
    "memory_episode_sessions",
    "memory_record_versions",
    "memory_records",
    "memory_profiles",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    for name in _TABLES:
        if _has_table(name):
            op.drop_table(name)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        Base.metadata.tables[name].create(bind, checkfirst=True)
