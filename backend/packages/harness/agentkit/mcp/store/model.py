"""ORM tables for the owner-private MCP server rows and their derived tools.

`mcp_servers` is the authoritative configuration of one user's MCP server: the
derived tools (`mcp_tools`) and the Milvus retrieval collection are both
rebuildable projections of it, so this row is the only thing a user actually edits.

Uniqueness is `(owner_user_id, name)`: two users may each own an `anysearch`, and a
single user cannot own two.  `name` is part of every derived call name, so it is
never renamed in place - the row is deleted and rebuilt instead.

`mcp_tools` holds one row per tool the server advertises.  It inherits the owner from
the server (there is no tool-level sharing), so its uniqueness is
`(server_id, name)` plus `(owner_user_id, call_name)`: two servers of two users may
both offer `search`, but one user cannot end up with two tools behind one name.
`server_name` is denormalised on purpose - the slug is immutable, and the search
card renders it without joining back to `mcp_servers`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
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


class McpToolRow(Base):
    """One tool of one server. The row survives the tool disappearing upstream."""

    __tablename__ = "mcp_tools"

    #: uuid5(NAMESPACE_URL, "<server_id>:<name>"): stable, and the Milvus pk.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The owning `mcp_servers.id`; deleting the server deletes these rows.
    server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The owning server's immutable slug, copied for the search cards.
    server_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Inherited from the server. Tools are owner-private, never shared or claimed.
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The remote tool name, exactly as the server advertises it.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Model/policy-facing name: "mcp__<server_name>__<name>".
    call_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Verbatim MCP inputSchema ($ref/anyOf/default preserved as sent).
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    annotations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: Parameter names derived from input_schema, for cards and BM25 text.
    parameters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: BM25/embedding text; rebuilt by the sync, stored verbatim here.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: sha256(content + owner + call_name): a server or owner move re-indexes the tool.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: sha256(canonical input_schema): schema edits never re-embed.
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Inherited from the server's last initialize.
    reachable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Runtime gate for tool_search/tool_execute; false keeps the definition only.
    callable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Last time this tool appeared in that server's tools/list.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("server_id", "name", name="uq_mcp_tools_server_name"),
        UniqueConstraint("owner_user_id", "call_name", name="uq_mcp_tools_owner_call_name"),
        Index("ix_mcp_tools_owner_callable", "owner_user_id", "callable"),
        Index("ix_mcp_tools_server_callable", "server_id", "callable"),
    )


__all__ = ["McpServerRow", "McpToolRow"]
