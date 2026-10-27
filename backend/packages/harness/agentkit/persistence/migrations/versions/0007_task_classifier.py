"""Add task context columns.

Revision ID: 0007_task_classifier
Revises: 0006_agent_memory

SQLite uses batch mode because it cannot ALTER COLUMN.  The migration is
re-entrant: create_all may already have created these columns on a fresh test
database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_task_classifier"
down_revision = "0006_agent_memory"
branch_labels = None
depends_on = None

_SESSION_COLUMNS = (
    ("task_id", sa.String(length=36), True),
    ("task_goal", sa.Text(), True),
    ("task_goal_source", sa.String(length=16), True),
    ("boundary_decision", sa.String(length=16), False),
    ("boundary_uncertain", sa.Boolean(), False),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    existing = _columns("memory_episode_sessions")
    with op.batch_alter_table("memory_episode_sessions") as batch:
        for name, type_, nullable in _SESSION_COLUMNS:
            if name not in existing:
                batch.add_column(
                    sa.Column(
                        name,
                        type_,
                        nullable=nullable,
                        server_default=sa.false() if name == "boundary_uncertain" else None,
                    )
                )

    op.execute(
        "UPDATE memory_episode_sessions "
        "SET task_id = id WHERE task_id IS NULL OR task_id = ''"
    )
    op.execute("UPDATE memory_episode_sessions SET task_goal = '' WHERE task_goal IS NULL")
    op.execute(
        "UPDATE memory_episode_sessions "
        "SET task_goal_source = 'fallback' WHERE task_goal_source IS NULL"
    )

    with op.batch_alter_table("memory_episode_sessions") as batch:
        batch.alter_column("task_id", existing_type=sa.String(length=36), nullable=False)
        batch.alter_column("task_goal", existing_type=sa.Text(), nullable=False)
        batch.alter_column("task_goal_source", existing_type=sa.String(length=16), nullable=False)
        batch.alter_column(
            "boundary_uncertain",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )

    record_columns = _columns("memory_records")
    if "task_id" not in record_columns:
        with op.batch_alter_table("memory_records") as batch:
            batch.add_column(sa.Column("task_id", sa.String(length=36), nullable=True))

    session_indexes = _indexes("memory_episode_sessions")
    if "ix_memory_episode_sessions_user_task" not in session_indexes:
        op.create_index(
            "ix_memory_episode_sessions_user_task",
            "memory_episode_sessions",
            ["user_id", "task_id"],
        )
    record_indexes = _indexes("memory_records")
    if "ix_memory_records_user_task" not in record_indexes:
        op.create_index(
            "ix_memory_records_user_task",
            "memory_records",
            ["user_id", "task_id"],
        )


def downgrade() -> None:
    session_indexes = _indexes("memory_episode_sessions")
    if "ix_memory_episode_sessions_user_task" in session_indexes:
        op.drop_index("ix_memory_episode_sessions_user_task", table_name="memory_episode_sessions")
    record_indexes = _indexes("memory_records")
    if "ix_memory_records_user_task" in record_indexes:
        op.drop_index("ix_memory_records_user_task", table_name="memory_records")

    existing = _columns("memory_episode_sessions")
    with op.batch_alter_table("memory_episode_sessions") as batch:
        for name, _type, _nullable in reversed(_SESSION_COLUMNS):
            if name in existing:
                batch.drop_column(name)

    record_columns = _columns("memory_records")
    if "task_id" in record_columns:
        with op.batch_alter_table("memory_records") as batch:
            batch.drop_column("task_id")
