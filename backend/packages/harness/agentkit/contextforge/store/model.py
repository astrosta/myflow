"""ORM table for the authoritative ContextForge tool catalog.

``contextforge_tools`` is the runtime source of truth: the Milvus collection is a
derived retrieval index rebuilt from these rows, and tool_execute resolves a
``call_name`` back to the stored ``input_schema`` here before touching ContextForge.

PostgreSQL is authoritative in production; SQLite is the test backend.  The
uniqueness the sync path depends on (name, call_name) is expressed as table
constraints named here, so the migration and the ORM cannot drift apart.
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


class ContextForgeToolRow(Base):
    """One ContextForge tool. The row survives the tool disappearing upstream."""

    __tablename__ = "contextforge_tools"

    #: ContextForge's own tool id; the join key Milvus stores as provider.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: MCP tool name; the name ContextForge MCP tools/call receives.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Model/policy-facing name: "cf__" + name.
    call_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Verbatim MCP inputSchema ($ref/anyOf/default preserved as sent).
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    annotations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Parameter names derived from input_schema, for search cards and BM25 text.
    parameters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: From annotations.myflow_owner; NULL means unowned and never callable.
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    reachable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Runtime gate for tool_search/tool_execute; false keeps the definition only.
    callable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: BM25/embedding text; rebuilt by the sync service, stored verbatim here.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: sha256(content + owner + call_name): an owner move re-indexes the tool.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: sha256(canonical input_schema): schema edits never re-embed.
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Last time the tool appeared in ContextForge tools/list.
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
        UniqueConstraint("name", name="uq_contextforge_tools_name"),
        UniqueConstraint("call_name", name="uq_contextforge_tools_call_name"),
        Index("ix_contextforge_tools_owner_callable", "owner_user_id", "callable"),
        Index("ix_contextforge_tools_callable", "callable"),
        Index("ix_contextforge_tools_last_seen_at", "last_seen_at"),
    )


__all__ = ["ContextForgeToolRow"]
