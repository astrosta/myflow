"""Drop the retired profile-suggestion table.

Revision ID: 0016_drop_profile_suggestions
Revises: 0015_skill_index_repair

Profile suggestions were replaced by the human-in-the-loop confirmation on the
`update_user_profile` tool. The table is no longer written or read; this
migration removes it on existing deployments. Dropping it is intentionally
irreversible: the stored proposals have no consumer after this change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_drop_profile_suggestions"
down_revision = "0015_skill_index_repair"
branch_labels = None
depends_on = None

_TABLE = "memory_profile_suggestions"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)


def downgrade() -> None:
    # Irreversible by design: the rows are proposal metadata with no consumer
    # and cannot be reconstructed from the current schema.
    pass
