"""Drop the retired external-gateway tool catalog table.

Revision ID: 0020_drop_old_contextforge_tools
Revises: 0019_mcp_tools

MyFlow-owned MCP servers replaced the external gateway, and `mcp_tools` replaced
`contextforge_tools`.  No row of the retired catalog is migrated on purpose: the
old rows were keyed by a gateway tool id, had no owning MCP server, and the
Milvus collection that indexed them (`myflow_contextforge_tools`) is discarded
with them.  The new catalog is rebuilt from each server's `tools/list`.

`upgrade()` is re-entrant: the table may already be gone on a database that
never created it, and `downgrade()` is deliberately a no-op - recreating an
empty old table would not restore anything and would only make the schema look
like the retired path still exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_drop_old_contextforge_tools"
down_revision = "0019_mcp_tools"
branch_labels = None
depends_on = None

_TABLE = "contextforge_tools"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(_TABLE):
        op.drop_table(_TABLE)


def downgrade() -> None:
    """Intentionally nothing: the retired catalog is never recreated."""


__all__ = ["revision", "down_revision", "upgrade", "downgrade"]
