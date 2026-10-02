"""Append-only ledger row. The public cursor is (thread_id, seq), not id."""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentkit.persistence.base import Base
from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)


class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(RUN_EVENT_TYPE_MAX_LENGTH), nullable=False)
    category: Mapped[str] = mapped_column(String(RUN_EVENT_CATEGORY_MAX_LENGTH), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
