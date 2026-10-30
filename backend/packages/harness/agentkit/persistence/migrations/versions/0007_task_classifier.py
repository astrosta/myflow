"""Historical canonical task-context migration (retired).

Revision ID: 0007_task_classifier
Revises: 0006_agent_memory

The canonical tables no longer exist; this revision is a no-op retained for
Alembic chain compatibility.
"""

from __future__ import annotations

revision = "0007_task_classifier"
down_revision = "0006_agent_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    return


def downgrade() -> None:
    return
