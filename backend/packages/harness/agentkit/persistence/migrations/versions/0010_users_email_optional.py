"""Make users.email optional now that username is the login identifier.

Revision ID: 0010_users_email_optional
Revises: 0009_drop_canonical_memory

Registration creates accounts without an email, while existing accounts keep
their address.  The unique email index stays in place; SQLite and PostgreSQL
both allow multiple NULL values in a unique column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_users_email_optional"
down_revision = "0009_drop_canonical_memory"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    if "users" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "email" not in _columns():
        return
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.alter_column(
            "email",
            existing_type=sa.String(length=254),
            nullable=True,
        )


def downgrade() -> None:
    if "users" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if "email" not in _columns():
        return
    # Older schema requires an address. Give placeholder addresses to the rows
    # created after the switch; usernames are already unique.
    op.execute(
        sa.text(
            "UPDATE users SET email = username || '@users.invalid' "
            "WHERE email IS NULL"
        )
    )
    with op.batch_alter_table("users", recreate="always") as batch:
        batch.alter_column(
            "email",
            existing_type=sa.String(length=254),
            nullable=False,
        )
