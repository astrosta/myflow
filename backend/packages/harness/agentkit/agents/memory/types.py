"""Shared memory types and the bounded user-profile schema (M10 section 2.2-2.3).

This module is deliberately storage-agnostic: pydantic models only, no LangGraph or
LangChain imports. Backends persist MemoryRecord envelopes; services own the type schemas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MemoryKind:
    """Known kind constants. Kinds are strings so extensions can register new ones."""

    PROFILE = "profile"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


RecordStatus = Literal[
    "active",
    "superseded",
    "folded",
    "archived",
    "deleted",
    "conflicted",
    "draft",
    "deprecated",
]
ProfileSource = Literal[
    "user_manual",
    "user_explicit_durable",
    "user_confirmed",
    "inferred",
]

#: Only these sources may become resident profile fields.
PROFILE_RESIDENT_SOURCES = frozenset({
    "user_manual",
    "user_explicit_durable",
    "user_confirmed",
})
EXPLICIT_ONLY_FIELDS = frozenset({
    "identity.preferred_name",
    "identity.pronouns",
    "accessibility.needs",
    "boundaries",
})
SENSITIVE_FIELDS = EXPLICIT_ONLY_FIELDS
PROFILE_FIELD_PRIORITY: tuple[str, ...] = (
    "identity.preferred_name",
    "identity.pronouns",
    "identity.locale",
    "identity.timezone",
    "communication.language",
    "accessibility.needs",
    "boundaries",
    "communication.tone",
    "communication.verbosity",
    "communication.format",
    "work.role",
    "work.domains",
    "work.tools",
    "work.expertise",
)
PROFILE_FIELD_PATHS = frozenset(PROFILE_FIELD_PRIORITY)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)


class MemoryKindSpec(BaseModel):
    """Metadata only. Behavior lives in the service."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    display_name: str
    schema_ref: str
    searchable: bool = True
    injectable: bool = False
    evictable: bool = True
    versioned: bool = False
    default_ttl_days: int | None = None
    description: str = ""


class MemoryRecord(BaseModel):
    """The single storage envelope shared by every kind."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    kind: str
    subtype: str | None = None
    content: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    search_text: str | None = None
    source: str = "conversation"
    confidence: float = 1.0
    status: RecordStatus = "active"
    revision: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    topic_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_name: str | None = None
    thread_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    explicit_remember: bool = False
    pinned: bool = False
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at", "valid_from", "valid_to", "archived_at", "deleted_at")
    @classmethod
    def _dates_are_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_validator("content", "search_text")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.replace("\x00", "")

    @model_validator(mode="after")
    def _profile_id_is_stable(self) -> "MemoryRecord":
        if self.kind == MemoryKind.PROFILE and self.id != "profile":
            raise ValueError("profile records must use id='profile'")
        return self


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: list[str] | None = None
    text: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    time_range: tuple[datetime, datetime] | None = None
    limit: int = Field(default=8, ge=1, le=100)
    cursor: str | None = None
    include_history: bool = False
    sort: Literal["relevance", "recency", "vitality"] = "relevance"


class MemoryHit(BaseModel):
    record: MemoryRecord
    score: float | None = None
    highlights: list[str] = Field(default_factory=list)


class MemorySearchPage(BaseModel):
    items: list[MemoryHit] = Field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryScope:
    user_id: str
    agent_name: str | None = None
    thread_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("MemoryScope.user_id must be a non-empty string")


class MemoryStats(BaseModel):
    user_id: str
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    last_maintenance_at: datetime | None = None
    last_eviction_at: datetime | None = None
    last_fold_at: datetime | None = None
    dirty: bool = False
    # Additive maintenance bookkeeping. Not part of the wire contract's required fields.
    search_calls: int = 0
    low_vitality_cycles: dict[str, int] = Field(default_factory=dict)

    @field_validator("last_maintenance_at", "last_eviction_at", "last_fold_at")
    @classmethod
    def _dates_are_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class ScopePage(BaseModel):
    items: list[MemoryScope] = Field(default_factory=list)
    next_cursor: str | None = None


class Resolution(BaseModel):
    action: Literal["accept", "reject", "conflict", "supersede"]
    record: MemoryRecord | None = None
    reason: str = ""


class MemoryInstruction(BaseModel):
    operation: Literal["forget", "supersede", "unprotect", "remember"]
    target_record_id: str | None = None
    topic_key: str | None = None
    evidence: str
    source: Literal["conversation", "ui"]
    created_at: datetime = Field(default_factory=utc_now)


class MemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    subtype: str | None = None
    operation: Literal["upsert", "forget", "supersede", "unprotect", "remember"] = "upsert"
    payload: dict[str, Any] = Field(default_factory=dict)
    target_record_id: str | None = None
    evidence: str = ""
    source: Literal["conversation", "ui", "import"] = "conversation"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    expected_revision: int | None = None

    @model_validator(mode="after")
    def _instruction_has_target(self) -> "MemoryProposal":
        if self.operation != "upsert" and not self.target_record_id:
            raise ValueError(f"{self.operation} proposals require target_record_id")
        if self.operation == "upsert" and not isinstance(self.payload, dict):
            raise ValueError("upsert proposals require an object payload")
        return self


class MemoryBackendCapabilities(BaseModel):
    vector_search: bool = True
    metadata_filter: bool = True
    batch_write: bool = True
    cas: bool = True
    list_scopes: bool = True
    ttl: bool = False


# ---------------------------------------------------------------------------
# Profile schema
# ---------------------------------------------------------------------------

T = TypeVar("T")


class ProfileField(BaseModel, Generic[T]):
    """A bounded, sourced profile value. The schema is the only profile storage shape."""

    model_config = ConfigDict(extra="forbid")

    value: T
    source: ProfileSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    observed_at: datetime = Field(default_factory=utc_now)
    confirmed_at: datetime | None = None
    pinned: bool = False

    @field_validator("observed_at", "confirmed_at")
    @classmethod
    def _dates_are_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _inferred_is_never_resident(self) -> "ProfileField":
        if self.source == "inferred":
            raise ValueError("inferred values are facts, never profile fields")
        return self


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_name: ProfileField[str] | None = None
    pronouns: ProfileField[str] | None = None
    locale: ProfileField[str] | None = None
    timezone: ProfileField[str] | None = None


class Communication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: ProfileField[str] | None = None
    tone: ProfileField[Literal["neutral", "friendly", "formal", "direct"]] | None = None
    verbosity: ProfileField[Literal["concise", "balanced", "detailed"]] | None = None
    format: ProfileField[list[Literal["bullets", "tables", "steps", "prose", "code_first"]]] | None = None


class WorkContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ProfileField[str] | None = None
    domains: ProfileField[list[str]] | None = None
    tools: ProfileField[list[str]] | None = None
    expertise: ProfileField[Literal["beginner", "intermediate", "expert"]] | None = None


class Accessibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs: ProfileField[list[str]] | None = None


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    revision: int = 0
    updated_at: datetime = Field(default_factory=utc_now)

    identity: Identity = Field(default_factory=Identity)
    communication: Communication = Field(default_factory=Communication)
    work: WorkContext = Field(default_factory=WorkContext)
    accessibility: Accessibility = Field(default_factory=Accessibility)

    boundaries: ProfileField[list[str]] | None = None

    @field_validator("updated_at")
    @classmethod
    def _updated_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProfileSnapshot(BaseModel):
    profile: UserProfile
    schema_version: int
    revision: int
    token_count: int
    truncated: bool = False


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    source: Literal["user_manual", "user_explicit_durable", "user_confirmed"]
    evidence: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Type payloads
# ---------------------------------------------------------------------------


class SemanticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    category: str = "preference"
    topic_key: str | None = None
    confidence: float = 1.0
    source: Literal["user_explicit", "user_confirmed", "inferred", "consolidated", "import"]
    status: RecordStatus = "active"
    explicit_remember: bool = False
    profile_linked: str | None = None
    access_count: int = 0
    confirmation_count: int = 0
    last_accessed_at: datetime | None = None
    last_confirmed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_fact_ids: list[str] = Field(default_factory=list)
    fold_depth: int = 0


class EpisodicPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    occurred_at: datetime = Field(default_factory=utc_now)
    thread_id: str | None = None
    outcome: str | None = None
    entities: list[str] = Field(default_factory=list)


class ProcedurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = 1
    steps: list[str] = Field(default_factory=list)
    tool_refs: list[str] = Field(default_factory=list)
    approval: Literal["draft", "approved", "rejected"] = "draft"
    status: Literal["draft", "active", "deprecated"] = "draft"
    description: str = ""


class MemoryConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    kind: str
    subtype: str | None = None
    field_path: str | None = None
    topic_key: str | None = None
    current_record_id: str | None = None
    candidate_record_id: str | None = None
    current_source: str
    candidate_source: str
    evidence: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "dismissed"] = "open"
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def _created_at_is_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class MemoryExport(BaseModel):
    schema_version: int = 1
    exported_at: datetime = Field(default_factory=utc_now)
    profile: dict[str, Any] | None = None
    records: list[MemoryRecord] = Field(default_factory=list)
    conflicts: list[MemoryConflict] = Field(default_factory=list)
    maintenance: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MemoryManagerError(RuntimeError):
    """Base class for memory failures surfaced to the gateway."""


class MemoryReadError(MemoryManagerError):
    pass


class MemoryWriteError(MemoryManagerError):
    pass


class MemoryConflictError(MemoryManagerError):
    """A record/profile revision changed under a writer. This is HTTP 409."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class MemoryKindNotSupported(MemoryManagerError):
    pass


class MemoryCorruptionError(MemoryManagerError):
    pass


class MemoryNotFoundError(MemoryManagerError):
    pass


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

_VALID_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_VALID_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)*$")


def profile_field(profile: UserProfile, path: str) -> ProfileField | None:
    if path not in PROFILE_FIELD_PATHS:
        raise ValueError(f"unknown profile field: {path}")
    section, _, name = path.partition(".")
    if not name:
        return getattr(profile, section, None)
    return getattr(getattr(profile, section), name, None)


def set_profile_field(profile: UserProfile, path: str, value: ProfileField | None) -> UserProfile:
    if path not in PROFILE_FIELD_PATHS:
        raise ValueError(f"unknown profile field: {path}")
    updated = profile.model_copy(deep=True)
    section, _, name = path.partition(".")
    if not name:
        setattr(updated, section, value)
    else:
        setattr(getattr(updated, section), name, value)
    return updated


def _max_length(path: str) -> int:
    if path in {"identity.preferred_name"}:
        return 64
    if path in {"identity.pronouns"}:
        return 32
    if path in {"work.role"}:
        return 100
    return 256


def _validate_list(value: Any, *, path: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    if len(value) > max_items:
        raise ValueError(f"{path} accepts at most {max_items} items")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path} items must be non-empty strings")
        if len(item) > max_chars:
            raise ValueError(f"{path} items must be at most {max_chars} characters")
        cleaned.append(item.strip())
    return cleaned


def validate_profile_value(path: str, value: Any) -> Any:
    """Validate one bounded core field. Values are never free-form notes."""
    if path not in PROFILE_FIELD_PATHS:
        raise ValueError(f"unknown profile field: {path}")
    if path in EXPLICIT_ONLY_FIELDS and value is None:
        return None
    if path in {"communication.format"}:
        allowed = {"bullets", "tables", "steps", "prose", "code_first"}
        if not isinstance(value, list) or not value or len(value) > len(allowed):
            raise ValueError(f"{path} must be a non-empty list of known formats")
        if any(item not in allowed for item in value):
            raise ValueError(f"{path} contains an unknown format")
        return list(dict.fromkeys(value))
    if path in {"work.domains", "work.tools", "accessibility.needs", "boundaries"}:
        limits = {
            "work.domains": (10, 64),
            "work.tools": (20, 64),
            "accessibility.needs": (10, 120),
            "boundaries": (10, 200),
        }
        return _validate_list(value, path=path, max_items=limits[path][0], max_chars=limits[path][1])
    if path == "communication.tone":
        if value not in {"neutral", "friendly", "formal", "direct"}:
            raise ValueError("communication.tone must be neutral, friendly, formal, or direct")
        return value
    if path == "communication.verbosity":
        if value not in {"concise", "balanced", "detailed"}:
            raise ValueError("communication.verbosity must be concise, balanced, or detailed")
        return value
    if path == "work.expertise":
        if value not in {"beginner", "intermediate", "expert"}:
            raise ValueError("work.expertise must be beginner, intermediate, or expert")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{path} must not be empty")
    limit = _max_length(path)
    if len(text) > limit:
        raise ValueError(f"{path} must be at most {limit} characters")
    if path == "identity.locale" or path == "communication.language":
        if not _VALID_LOCALE.fullmatch(text):
            raise ValueError(f"{path} must be a BCP-47 language tag")
    if path == "identity.timezone":
        if not _VALID_TIMEZONE.fullmatch(text):
            raise ValueError("identity.timezone must be an IANA timezone")
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("identity.timezone must be a known IANA timezone") from exc
    if _looks_like_secret(text):
        raise ValueError(f"{path} looks like a credential and must not be stored")
    if "[REDACTED]" in text or "[[PII:" in text:
        raise ValueError(f"{path} must not contain a redaction marker")
    return text


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS)


def looks_like_secret(value: str) -> bool:
    """Public safety gate used by every kind that can store free text."""
    return _looks_like_secret(value)


def source_rank(source: str) -> int:
    """Higher wins. Profile sources and semantic sources share one practical order."""
    return {
        "user_manual": 4,
        "user_explicit": 3,
        "user_explicit_durable": 3,
        "user_confirmed": 2,
        "inferred": 1,
        "consolidated": 2,
        "import": 2,
        "assistant": 0,
        "tool": 0,
    }.get(source, 0)


def is_resident_source(source: str) -> bool:
    return source in PROFILE_RESIDENT_SOURCES
