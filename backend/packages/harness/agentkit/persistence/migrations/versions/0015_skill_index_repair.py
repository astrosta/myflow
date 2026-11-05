"""Track index repair attempts and backoff for one skill version.

Revision ID: 0015_skill_index_repair
Revises: 0014_skill_load_count
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_skill_index_repair"
down_revision = "0014_skill_load_count"
branch_labels = None
depends_on = None

_COLUMNS = {
    "index_attempts": sa.Column(
        "index_attempts", sa.Integer(), nullable=False, server_default="0"
    ),
    "index_last_error": sa.Column("index_last_error", sa.String(255), nullable=True),
    "index_next_attempt_at": sa.Column(
        "index_next_attempt_at", sa.DateTime(timezone=True), nullable=True
    ),
}


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(index["name"] == name for index in inspector.get_indexes(table))


def upgrade() -> None:
    existing = _columns("skill_versions")
    added = [column for name, column in _COLUMNS.items() if name not in existing]
    if added:
        with op.batch_alter_table("skill_versions") as batch:
            for column in added:
                batch.add_column(column)
    # 快路径每轮都按 index_status 过滤；没有索引就是全表扫。
    if not _has_index("skill_versions", "ix_skill_versions_index_status"):
        op.create_index(
            "ix_skill_versions_index_status", "skill_versions", ["index_status"]
        )


def downgrade() -> None:
    if _has_index("skill_versions", "ix_skill_versions_index_status"):
        op.drop_index("ix_skill_versions_index_status", table_name="skill_versions")
    existing = _columns("skill_versions")
    removed = [name for name in _COLUMNS if name in existing]
    if removed:
        with op.batch_alter_table("skill_versions") as batch:
            for name in removed:
                batch.drop_column(name)
