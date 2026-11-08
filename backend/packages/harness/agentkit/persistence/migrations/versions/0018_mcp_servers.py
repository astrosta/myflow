"""Create the owner-private MCP server table.

Revision ID: 0018_mcp_servers
Revises: 0017_contextforge_tools

Re-entrant like every other DDL revision in this chain: an empty database is built
by `Base.metadata.create_all` and stamped at head, so the table may already exist
when `upgrade()` runs.  There is no historical data to migrate - MCP server
management has not shipped - so the upgrade only creates the current shape.

The derived `mcp_tools` table and the deletion of the retired external-gateway
catalog arrive with the tool-catalog task, not here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import agentkit.mcp.store.model  # noqa: F401 - register metadata
from agentkit.persistence.base import Base

revision = "0018_mcp_servers"
down_revision = "0017_contextforge_tools"
branch_labels = None
depends_on = None

_TABLES = ("mcp_servers",)


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
