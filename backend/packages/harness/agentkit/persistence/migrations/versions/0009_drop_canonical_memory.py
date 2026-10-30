"""Drop the retired canonical memory tables.

Revision ID: 0009_drop_canonical_memory
Revises: 0008_openviking_memory

The runtime code is gone. This migration removes any tables left in databases
created before the OpenViking cutover. Downgrade is intentionally unavailable:
the canonical schema is permanently retired.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_drop_canonical_memory"
down_revision = "0008_openviking_memory"
branch_labels = None
depends_on = None

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
    return
