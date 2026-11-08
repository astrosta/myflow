"""ORM table for the owner-private MCP server rows.

`mcp_servers` is the authoritative configuration of one user's MCP server: the
derived tools (`mcp_tools`, next task) and the Milvus retrieval collection are both
rebuildable projections of it, so this row is the only thing a user actually edits.

Uniqueness is `(owner_user_id, name)`: two users may each own an `anysearch`, and a
single user cannot own two.  `name` is part of every derived call name, so it is
never renamed in place - the row is deleted and rebuilt instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class McpServerRow(Base):
    """One user's MCP server. The row survives an unreachable upstream."""

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: Creator. Every read and write is owner-scoped; there is no shared server.
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Stable slug ([a-z0-9-]+), unique per owner, immutable after creation.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: "streamable_http" or "sse"; stdio is out of scope.
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The MCP endpoint, including any query string the upstream requires.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Fernet token of the HTTP headers; NULL means "no headers". Never plaintext.
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: pending / active / error / disabled - derived from the last sync.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    #: Did the last initialize succeed?
    reachable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Truncated last failure, shown in the UI.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Last successful initialize; preserved across later failures.
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Last successful tools/list for this server.
    last_tools_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_mcp_servers_owner_name"),
        # The catalog and status pages read "this owner's enabled servers by status".
        Index("ix_mcp_servers_owner_enabled_status", "owner_user_id", "enabled", "status"),
    )


__all__ = ["McpServerRow"]
