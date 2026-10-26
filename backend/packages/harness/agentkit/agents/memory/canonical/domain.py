"""Storage-agnostic value objects for the canonical memory runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


class MemoryManagerError(RuntimeError):
    """Base class for canonical memory failures."""


class MemoryConflictError(MemoryManagerError):
    """A CAS revision or optimistic status check failed."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class MemoryNotFoundError(MemoryManagerError):
    """The requested canonical row does not exist for this user."""


class MemoryUnavailableError(MemoryManagerError):
    """A dependency (database/embedding/Milvus) is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class MutationContext:
    actor_type: Literal["SYSTEM", "USER", "ADMIN"]
    actor_id: str
    op_type: str
    reason: str | None = None
    source_episode_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordDraft:
    user_id: str
    id: str
    kind: str
    subtype: str | None
    status: str
    content: str
    payload: dict[str, Any]
    topic_key: str | None
    scope_condition: str
    source: str
    evidence_tier: str | None
    confidence: float
    salience: float
    explicit_remember: bool
    parent_memory_id: str | None
    correction_of: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    occurred_at: datetime | None
    thread_id: str | None
    agent_name: str | None
    provenance: dict[str, Any] = field(default_factory=dict)
    source_episode_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryProfileView:
    user_id: str
    schema_version: int
    revision: int
    profile: dict[str, Any]
    tombstones: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProfileFieldView:
    value: Any
    source: str
    evidence_tier: str | None
    confidence: float
    observed_at: datetime
    tombstoned_at: datetime | None = None

    def model_dump(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProfileSnapshotView:
    revision: int
    fields: dict[str, ProfileFieldView]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProfileApplyResult:
    applied: bool
    snapshot: ProfileSnapshotView
    conflict_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryHitView:
    record: MemoryRecordView
    score: float | None = None
    highlights: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemorySearchPage:
    items: list[MemoryHitView] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryConflictView:
    id: str
    user_id: str
    kind: str
    field_path: str | None
    topic_key: str | None
    current_record_id: str | None
    candidate_record_id: str | None
    current_snapshot: dict[str, Any]
    candidate_snapshot: dict[str, Any]
    status: str
    revision: int
    evidence_refs: list[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryRecordView:
    id: str
    user_id: str
    kind: str
    subtype: str | None
    status: str
    revision: int
    content: str
    payload: dict[str, Any]
    topic_key: str | None
    scope_condition: str
    source: str
    evidence_tier: str | None
    confidence: float
    consolidation_status: str | None
    consolidated_at: datetime | None
    salience: float
    hit_count: int
    last_active_at: datetime
    explicit_remember: bool
    parent_memory_id: str | None
    correction_of: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    occurred_at: datetime | None
    thread_id: str | None
    agent_name: str | None
    provenance: dict[str, Any]
    source_episode_ids: list[str]
    archived_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemorySessionView:
    id: str
    user_id: str
    thread_id: str
    status: str
    turn_refs: list[dict[str, Any]]
    episode_key: str | None
    last_activity_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryIngestJobView:
    id: str
    user_id: str
    thread_id: str
    episode_session_id: str
    episode_key: str
    status: str
    attempts: int
    payload: dict[str, Any]
    lease_until: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryOutboxView:
    id: str
    user_id: str
    pk: str
    memory_id: str
    op: str
    revision: int
    epoch: int
    state: str
    attempts: int


@dataclass(frozen=True, slots=True)
class MemoryLedgerView:
    tx_id: str
    user_id: str
    actor_type: str
    actor_id: str
    op_type: str
    before_id: str | None
    after_id: str | None
    before_revision: int | None
    after_revision: int | None
    before_status: str | None
    after_status: str | None
    op_json: dict[str, Any]
    inverse_json: dict[str, Any]
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PendingPage:
    items: list[MemoryRecordView]
    next_cursor: str | None = None
