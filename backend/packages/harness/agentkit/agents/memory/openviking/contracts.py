"""Wire-independent contracts for the OpenViking memory adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MemoryType = Literal[
    "preferences",
    "entities",
    "events",
    "cases",
    "trajectories",
    "experiences",
    "tools",
    "skills",
]
SignalKind = Literal[
    "TASK_BOUNDARY",
    "TASK_COMPLETE",
    "FAILURE_RECOVERY",
    "USER_CORRECTION",
    "EXPLICIT_REMEMBER",
    "SESSION_END",
    "NONE",
]
TurnDisposition = Literal["capture", "skip"]
CaptureKind = Literal["human", "ai", "tool", "control"]
CaptureRole = Literal["user", "assistant"]
RunStatus = Literal["success", "error", "timeout", "interrupted"]


@dataclass(frozen=True, slots=True)
class TurnDecision:
    disposition: TurnDisposition
    signals: tuple[SignalKind, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SessionRef:
    user_id: str
    thread_id: str
    ov_user: str
    ov_session_id: str
    stream_shard: int
    session_epoch: int
    session_incarnation: str


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    tool_name: str
    status: str
    duration_ms: float | None = None
    error_type: str | None = None
    retry_count: int = 0
    input_digest: str | None = None
    output_digest: str | None = None
    summary: str = ""


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    user_id: str
    thread_id: str
    turn_id: str
    message_id: str
    session_epoch: int
    session_incarnation: str
    sequence: int
    kind: CaptureKind
    role: CaptureRole | None
    message: dict[str, Any] = field(default_factory=dict)
    stream_id: str = ""
    stream_shard: int = 0
    signals: tuple[SignalKind, ...] = ()
    run_status: RunStatus | None = None
    has_tool_error: bool = False
    has_recovery: bool = False
    degraded: bool = False
    tool_evidence: ToolEvidence | None = None
    created_at: str = ""

    @property
    def session_key(self) -> tuple[str, str, int, str]:
        return (self.user_id, self.thread_id, self.session_epoch, self.session_incarnation)


@dataclass(frozen=True, slots=True)
class ProfileView:
    fields: dict[str, Any]
    etag: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MemoryHit:
    uri: str
    memory_type: MemoryType
    abstract: str
    score: float | None = None
    level: Literal["L0", "L1", "L2"] | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: Literal["published", "skipped", "dropped", "degraded"]
    stream_id: str | None = None
    stream_shard: int | None = None
    reason: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CommitResult:
    status: Literal["accepted", "skipped", "degraded", "failed"]
    reason: str = ""
    task_id: str | None = None
    archive_uri: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    healthy: bool
    degraded: bool
    pending_stream_messages: int = 0
    pending_missing_turn_ends: int = 0
    last_error: str | None = None
