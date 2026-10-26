"""Canonical memory HTTP surface.

This router is mounted before the legacy memory router. It talks only to the
PostgreSQL-backed MemoryService; the old BaseStore routes remain available for
migration/compatibility tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryNotFoundError,
    ProfileSnapshotView,
    SkillCandidateView,
)
from agentkit.runtime.user_context import DEFAULT_USER_ID
from backend.app.gateway.deps import get_memory_service as get_memory_service_dep
from backend.app.gateway.deps import require_scope
from backend.app.gateway.errors import (
    InvalidMemory,
    MemoryConflictAPI,
    MemoryKindUnsupported,
    MemoryNotFoundAPI,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/memory", tags=["memory-canonical"])

Service = Annotated[Any, Depends(get_memory_service_dep)]
Reader = Annotated[RunCaller, Depends(require_scope("memory:read"))]
Writer = Annotated[RunCaller, Depends(require_scope("memory:write"))]
SkillWriter = Annotated[RunCaller, Depends(require_scope("skills:write"))]
Admin = Annotated[RunCaller, Depends(require_scope("admin"))]


class ProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    expected_revision: int = Field(ge=0)


class ProfileFieldResponse(BaseModel):
    value: Any
    source: str
    evidence_tier: str | None = None
    confidence: float
    observed_at: str


class ProfileResponse(BaseModel):
    schema_version: int = 1
    revision: int
    fields: dict[str, ProfileFieldResponse] = Field(default_factory=dict)


class SearchHit(BaseModel):
    id: str
    kind: str
    subtype: str | None = None
    content: str
    topic_key: str | None = None
    confidence: float
    updated_at: str
    score: float | None = None


class SearchPage(BaseModel):
    items: list[SearchHit] = Field(default_factory=list)
    next_cursor: str | None = None


class PendingItem(BaseModel):
    id: str
    content: str
    confidence: float
    evidence_tier: str | None = None
    evidence_quote: str | None = None
    source: str
    created_at: str
    updated_at: str
    revision: int


class PendingPage(BaseModel):
    items: list[PendingItem] = Field(default_factory=list)
    next_cursor: str | None = None


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class RecordResponse(BaseModel):
    id: str
    kind: str
    subtype: str | None = None
    content: str
    status: str
    revision: int
    confidence: float
    topic_key: str | None = None
    created_at: str
    updated_at: str


class EpisodeResponse(BaseModel):
    id: str
    content: str
    status: str
    outcome: str | None = None
    occurred_at: str | None = None
    thread_id: str | None = None
    confidence: float
    evidence_refs: list[str] = Field(default_factory=list)


class EpisodePage(BaseModel):
    items: list[EpisodeResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class CorrectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)
    outcome: str = "SUCCESS"
    evidence_refs: list[str] = Field(default_factory=list)
    expected_revision: int = Field(ge=0)


class SkillCandidateResponse(BaseModel):
    id: str
    status: str
    revision: int
    private_name: str
    description: str | None = None
    trigger_text: str
    package_sha256: str
    source_episode_ids: list[str] = Field(default_factory=list)
    created_at: str


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class ConfirmResponse(BaseModel):
    candidate_id: str
    skill_id: str
    version_id: str
    origin_candidate_id: str


class DeletedResponse(BaseModel):
    deleted: str


class ClearProfileResponse(BaseModel):
    cleared: str


class ReindexResponse(BaseModel):
    reindexed: int


class RecordPage(BaseModel):
    items: list[RecordResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explicit: bool = True


class ConflictResponse(BaseModel):
    id: str
    kind: str
    field_path: str | None = None
    topic_key: str | None = None
    status: str
    revision: int


class ConflictPage(BaseModel):
    items: list[ConflictResponse] = Field(default_factory=list)


class ResolveConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    resolution: Literal["current", "candidate", "merge"]
    merged_content: str | None = None


class ExportResponse(BaseModel):
    schema_version: int = 1
    profile: ProfileResponse
    records: list[RecordResponse] = Field(default_factory=list)
    conflicts: list[ConflictResponse] = Field(default_factory=list)


class ClearedResponse(BaseModel):
    cleared: bool


class MaintenanceRunResponse(BaseModel):
    run_id: str
    dry_run: bool
    archived: int = 0
    folded: int = 0
    errors: list[str] = Field(default_factory=list)


def _service(service: Any) -> Any:
    if service is None:
        raise MemoryKindUnsupported("memory is not enabled")
    return service


def _user(caller: RunCaller) -> str:
    return caller.user_id or DEFAULT_USER_ID


def _profile(snapshot: ProfileSnapshotView) -> ProfileResponse:
    return ProfileResponse(
        revision=snapshot.revision,
        fields={
            path: ProfileFieldResponse(
                value=field.value,
                source=field.source,
                evidence_tier=field.evidence_tier,
                confidence=field.confidence,
                observed_at=field.observed_at.isoformat(),
            )
            for path, field in snapshot.fields.items()
        },
    )


def _record(record: Any) -> RecordResponse:
    return RecordResponse(
        id=record.id,
        kind=record.kind,
        subtype=record.subtype,
        content=record.content,
        status=record.status,
        revision=record.revision,
        confidence=record.confidence,
        topic_key=record.topic_key,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _candidate(candidate: SkillCandidateView) -> SkillCandidateResponse:
    return SkillCandidateResponse(
        id=candidate.id,
        status=candidate.status,
        revision=candidate.revision,
        private_name=candidate.private_name,
        description=candidate.description,
        trigger_text=candidate.trigger_text,
        package_sha256=candidate.package_sha256,
        source_episode_ids=candidate.source_episode_ids,
        created_at=candidate.created_at.isoformat(),
    )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(service: Service, caller: Reader) -> ProfileResponse:
    return _profile(await _service(service).get_profile(_user(caller)))


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    body: ProfilePatchRequest, service: Service, caller: Writer
) -> ProfileResponse:
    try:
        snapshot = await _service(service).patch_profile(
            _user(caller), body.fields, expected_revision=body.expected_revision
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except (ValueError, MemoryNotFoundError) as exc:
        raise InvalidMemory(str(exc)) from exc
    return _profile(snapshot)


@router.delete("/profile/{field_path:path}", response_model=ClearProfileResponse)
async def clear_profile_field(
    field_path: str,
    service: Service,
    caller: Writer,
    expected_revision: int | None = Query(default=None, ge=0),
) -> dict[str, str]:
    try:
        await _service(service).clear_profile_field(
            _user(caller), field_path, expected_revision=expected_revision
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except (ValueError, MemoryNotFoundError) as exc:
        raise InvalidMemory(str(exc)) from exc
    return {"cleared": field_path}


@router.get("/search", response_model=SearchPage)
async def search_memory(
    service: Service,
    caller: Reader,
    q: str = "",
    kinds: str | None = None,
    limit: int = Query(default=8, ge=1, le=100),
    cursor: str | None = None,
) -> SearchPage:
    selected = [part.strip() for part in kinds.split(",") if part.strip()] if kinds else ["semantic"]
    if "profile" in selected:
        raise InvalidMemory("profile is not a search kind")
    try:
        page = await _service(service).search(
            _user(caller), text=q, kinds=selected, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise InvalidMemory(str(exc)) from exc
    return SearchPage(
        items=[
            SearchHit(
                id=hit.record.id,
                kind=hit.record.kind,
                subtype=hit.record.subtype,
                content=hit.record.content,
                topic_key=hit.record.topic_key,
                confidence=hit.record.confidence,
                updated_at=hit.record.updated_at.isoformat(),
                score=hit.score,
            )
            for hit in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/pending", response_model=PendingPage)
async def list_pending(
    service: Service,
    caller: Reader,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
) -> PendingPage:
    page = await _service(service).list_pending(_user(caller), limit=limit, cursor=cursor)
    return PendingPage(
        items=[
            PendingItem(
                id=record.id,
                content=record.content,
                confidence=record.confidence,
                evidence_tier=record.evidence_tier,
                evidence_quote=(record.payload or {}).get("evidence_quote"),
                source=record.source,
                created_at=record.created_at.isoformat(),
                updated_at=record.updated_at.isoformat(),
                revision=record.revision,
            )
            for record in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.post("/items/semantic/{record_id}/promote", response_model=RecordResponse)
async def promote_semantic(
    record_id: str, body: PromoteRequest, service: Service, caller: Writer
) -> RecordResponse:
    try:
        record = await _service(service).promote(
            _user(caller), record_id, expected_revision=body.expected_revision
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemory(str(exc)) from exc
    return _record(record)


@router.delete("/items/{kind}/{record_id}", response_model=DeletedResponse)
async def delete_item(kind: str, record_id: str, service: Service, caller: Writer) -> DeletedResponse:
    try:
        await _service(service).delete_item(_user(caller), kind, record_id)
    except (ValueError, MemoryNotFoundError) as exc:
        raise InvalidMemory(str(exc)) from exc
    return DeletedResponse(deleted=record_id)


@router.get("/episodes", response_model=EpisodePage)
async def list_episodes(
    service: Service,
    caller: Reader,
    limit: int = Query(default=50, ge=1, le=100),
) -> EpisodePage:
    records = await _service(service).get_episodes(_user(caller), limit=limit)
    return EpisodePage(
        items=[
            EpisodeResponse(
                id=record.id,
                content=record.content,
                status=record.status,
                outcome=(record.payload or {}).get("final_outcome"),
                occurred_at=record.occurred_at.isoformat() if record.occurred_at else None,
                thread_id=record.thread_id,
                confidence=record.confidence,
                evidence_refs=list((record.payload or {}).get("evidence_refs") or []),
            )
            for record in records
        ]
    )


@router.post("/episodes/{episode_id}/correct", response_model=RecordResponse)
async def correct_episode(
    episode_id: str, body: CorrectRequest, service: Service, caller: Writer
) -> RecordResponse:
    try:
        record = await _service(service).correct_episode(
            _user(caller),
            episode_id,
            content=body.content,
            outcome=body.outcome,
            evidence_refs=body.evidence_refs,
            expected_revision=body.expected_revision,
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemory(str(exc)) from exc
    return _record(record)


@router.get("/items", response_model=RecordPage)
async def list_items(
    service: Service,
    caller: Reader,
    kind: str = Query(...),
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> RecordPage:
    records = await _service(service).list_items(
        _user(caller), kind=kind, status=status, limit=limit
    )
    return RecordPage(items=[_record(record) for record in records])


@router.get("/items/{kind}/{record_id}", response_model=RecordResponse)
async def get_item(kind: str, record_id: str, service: Service, caller: Reader) -> RecordResponse:
    record = await _service(service).get_item(_user(caller), record_id)
    if record is None or record.kind != kind:
        raise MemoryNotFoundAPI("memory item not found")
    return _record(record)


@router.post("/items/{kind}/{record_id}/remember", response_model=RecordResponse)
async def remember_item(
    kind: str, record_id: str, body: RememberRequest, service: Service, caller: Writer
) -> RecordResponse:
    record = await _service(service).set_explicit(
        _user(caller), kind, record_id, body.explicit
    )
    if record is None:
        raise MemoryNotFoundAPI("memory item not found")
    return _record(record)


@router.get("/conflicts", response_model=ConflictPage)
async def list_conflicts(service: Service, caller: Reader) -> ConflictPage:
    items = await _service(service).list_conflicts(_user(caller))
    return ConflictPage(
        items=[
            ConflictResponse(
                id=item.id,
                kind=item.kind,
                field_path=item.field_path,
                topic_key=item.topic_key,
                status=item.status,
                revision=item.revision,
            )
            for item in items
        ]
    )


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictResponse)
async def resolve_conflict(
    conflict_id: str,
    body: ResolveConflictRequest,
    service: Service,
    caller: Writer,
) -> ConflictResponse:
    try:
        item = await _service(service).resolve_conflict(
            _user(caller),
            conflict_id,
            expected_revision=body.expected_revision,
            resolution=body.resolution,
            merged_content=body.merged_content,
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    return ConflictResponse(
        id=item.id,
        kind=item.kind,
        field_path=item.field_path,
        topic_key=item.topic_key,
        status=item.status,
        revision=item.revision,
    )


@router.get("/export", response_model=ExportResponse)
async def export_memory(service: Service, caller: Reader) -> ExportResponse:
    exported = await _service(service).export(_user(caller))
    return ExportResponse(
        profile=_profile(exported["profile"]),
        records=[_record(record) for record in exported["records"]],
        conflicts=[
            ConflictResponse(
                id=item.id,
                kind=item.kind,
                field_path=item.field_path,
                topic_key=item.topic_key,
                status=item.status,
                revision=item.revision,
            )
            for item in exported["conflicts"]
        ],
    )


@router.delete("", response_model=ClearedResponse)
async def clear_memory(service: Service, caller: Writer) -> ClearedResponse:
    await _service(service).clear(_user(caller))
    return ClearedResponse(cleared=True)


@router.post("/maintenance/run", response_model=MaintenanceRunResponse)
async def run_maintenance(
    service: Service, caller: Admin, dry_run: bool = True
) -> MaintenanceRunResponse:
    del service, caller
    return MaintenanceRunResponse(run_id="canonical", dry_run=dry_run)


@router.get("/skill-candidates", response_model=list[SkillCandidateResponse])
async def list_skill_candidates(
    service: Service, caller: Reader, status: str | None = None
) -> list[SkillCandidateResponse]:
    return [
        _candidate(candidate)
        for candidate in await _service(service).list_skill_candidates(
            _user(caller), status=status
        )
    ]


@router.post("/skill-candidates/{candidate_id}/confirm", response_model=ConfirmResponse)
async def confirm_skill_candidate(
    candidate_id: str, body: ConfirmRequest, service: Service, caller: SkillWriter
) -> ConfirmResponse:
    try:
        result = await _service(service).confirm_skill_candidate(
            _user(caller),
            candidate_id,
            expected_revision=body.expected_revision,
            package_sha256=body.package_sha256,
            idempotency_key=body.idempotency_key,
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemory(str(exc)) from exc
    return ConfirmResponse(
        candidate_id=result.candidate_id,
        skill_id=result.skill_id,
        version_id=result.version_id,
        origin_candidate_id=result.origin_candidate_id,
    )


@router.post("/skill-candidates/{candidate_id}/reject", response_model=SkillCandidateResponse)
async def reject_skill_candidate(
    candidate_id: str, body: RejectRequest, service: Service, caller: SkillWriter
) -> SkillCandidateResponse:
    try:
        candidate = await _service(service).reject_skill_candidate(
            _user(caller),
            candidate_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
    except MemoryConflictError as exc:
        raise MemoryConflictAPI(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    return _candidate(candidate)


@router.post("/reindex", response_model=ReindexResponse)
async def reindex(service: Service, caller: Admin) -> dict[str, int]:
    del caller
    return {"reindexed": await _service(service).reindex()}
