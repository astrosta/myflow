"""Add the canonical memory tables.

Revision ID: 0006_agent_memory
Revises: 0005_skill_catalog

The revision is intentionally re-entrant, matching 0004/0005: an empty
database may already have been created by Base.metadata.create_all and
stamped at head before this upgrade runs.  The table objects are defined once
in canonical.models; this migration creates the exact same schema and never
mutates the old skill/knowledge tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.agents.memory.canonical.models  # noqa: F401 - register metadata
from agentkit.persistence.schema import Base

revision = "0006_agent_memory"
down_revision = "0005_skill_catalog"
branch_labels = None
depends_on = None

_TABLES = (
    "memory_profiles",
    "memory_records",
    "memory_record_versions",
    "memory_episode_sessions",
    "memory_ingest_jobs",
    "memory_consolidation_jobs",
    "memory_skill_candidates",
    "memory_conflicts",
    "memory_mutation_ledger",
    "memory_index_outbox",
    "memory_scope_meta",
    "memory_projection_state",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES:
        table = Base.metadata.tables[name]
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if _has_table(name):
            Base.metadata.tables[name].drop(bind, checkfirst=True)
