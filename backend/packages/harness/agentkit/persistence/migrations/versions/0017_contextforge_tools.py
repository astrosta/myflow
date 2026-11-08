"""Add the retired external-gateway tool catalog table.

Revision ID: 0017_contextforge_tools
Revises: 0016_drop_profile_suggestions

This revision is re-entrant like every other DDL revision in this chain: an
empty database is built by Base.metadata.create_all and stamped at head, so the
table may already exist when upgrade() runs.  There is no historical data to
backfill - the external gateway never shipped - so the upgrade only creates the
current shape.

The table definition is **inline on purpose**.  The runtime package that owned
this catalog has been deleted; migration 0020 drops the table again.  Keeping
the old columns here, and nowhere else, is what lets a database that still
carries this revision upgrade and downgrade without importing dead modules.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_contextforge_tools"
down_revision = "0016_drop_profile_suggestions"
branch_labels = None
depends_on = None

_TABLE = "contextforge_tools"

#: The retired catalog, frozen at the shape the ORM used to declare.  Only DDL
#: matters here: defaults, ORM attributes and relationships are gone with the
#: runtime package.
_RETIRED_CATALOG = sa.Table(
    _TABLE,
    sa.MetaData(),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("call_name", sa.String(255), nullable=False),
    sa.Column("display_name", sa.String(512), nullable=True),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("input_schema", sa.JSON(), nullable=False),
    sa.Column("annotations", sa.JSON(), nullable=False),
    sa.Column("tags", sa.JSON(), nullable=False),
    sa.Column("parameters", sa.JSON(), nullable=False),
    sa.Column("owner_user_id", sa.String(64), nullable=True),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("reachable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("callable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("schema_hash", sa.String(64), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("name", name="uq_contextforge_tools_name"),
    sa.UniqueConstraint("call_name", name="uq_contextforge_tools_call_name"),
    sa.Index("ix_contextforge_tools_owner_callable", "owner_user_id", "callable"),
    sa.Index("ix_contextforge_tools_callable", "callable"),
    sa.Index("ix_contextforge_tools_last_seen_at", "last_seen_at"),
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    _RETIRED_CATALOG.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    if _has_table(_TABLE):
        _RETIRED_CATALOG.drop(op.get_bind())


__all__ = ["revision", "down_revision", "upgrade", "downgrade"]
