"""Create the derived MCP tool snapshot table.

Revision ID: 0019_mcp_tools
Revises: 0018_mcp_servers

Re-entrant like every other DDL revision in this chain: an empty database is built by
`Base.metadata.create_all` and stamped at head, so the table may already exist when
`upgrade()` runs.  There is no historical data to migrate - no tool row predates
MyFlow-owned MCP server management - so the upgrade only creates the current shape.

The retired external-gateway catalog (`contextforge_tools`) is deliberately left in
place here: dropping it belongs to the cleanup revision, which runs only after every
reader has moved to `mcp_tools`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.mcp.store.model  # noqa: F401 - register metadata
from agentkit.persistence.base import Base

revision = "0019_mcp_tools"
down_revision = "0018_mcp_servers"
branch_labels = None
depends_on = None

_TABLES = ("mcp_tools",)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    for name in _TABLES:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in reversed(_TABLES):
        if _has_table(name):
            Base.metadata.tables[name].drop(op.get_bind())


__all__ = ["revision", "down_revision", "upgrade", "downgrade"]
