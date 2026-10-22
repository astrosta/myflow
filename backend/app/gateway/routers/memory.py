"""Memory HTTP surface (M10 section 2.12). prefix=/memory, /api added by create_app."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agentkit.agents.memory.manager import MemoryManager, get_memory_manager
from agentkit.agents.memory.maintenance.service import MemoryMaintenanceService
from agentkit.agents.memory.types import (
    PROFILE_FIELD_PATHS,
    MemoryConflict,
    MemoryConflictError,
    MemoryCorruptionError,
    MemoryKind,
    MemoryKindNotSupported,
    MemoryNotFoundError,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    ProfilePatch,
    ProfileSnapshot,
)
from agentkit.runtime.user_context import DEFAULT_USER_ID
from backend.app.gateway.deps import get_memory_manager_service, require_scope
from backend.app.gateway.errors import (
    InvalidMemoryProfile,
    MemoryConflictAPI,
    MemoryCorrupted,
    MemoryKindUnsupported,
    MemoryNotFoundAPI,
    MemoryRequestInvalid,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(
    prefix="/memory",
    tags=["memory"],
    dependencies=[Depends(get_memory_manager_service)],
)


class ProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    expected_revision: int


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explicit: bool = True


class ConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal["current", "candidate", "merge"]
    merged_content: str | None = None


class MemoryRecordResponse(BaseModel):
    id: str
    kind: str
    subtype: str | None = None
    content: str
    structured: dict[str, Any] = Field(default_factory=dict)
    status: str
    revision: int
    explicit_remember: bool
    created_at: str
    updated_at: str


class FactResponse(BaseModel):
    id: str
    content: str
    category: str
    topic_key: str | None = None
    confidence: float
    source: str
    status: str
    explicit_remember: bool
    created_at: str
    updated_at: str


class ProfileResponse(BaseModel):
    schema_version: int
    revision: int
    fields: dict[str, Any] = Field(default_factory=dict)


class ConflictResponse(BaseModel):
    id: str
    kind: str
    field_path: str | None = None
    status: str


class RecordPage(BaseModel):
    items: list[MemoryRecordResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class FactPage(BaseModel):
    items: list[FactResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class ConflictPage(BaseModel):
    items: list[ConflictResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class MemoryExport(BaseModel):
    schema_version: int
    exported_at: str
    profile: ProfileResponse | None = None
    records: list[MemoryRecordResponse] = Field(default_factory=list)
    conflicts: list[ConflictResponse] = Field(default_factory=list)
    maintenance: dict[str, Any] = Field(default_factory=dict)


class MemoryResponse(BaseModel):
    schema_version: int
    profile: ProfileResponse | None = None
    records: list[MemoryRecordResponse] = Field(default_factory=list)
    conflicts: list[ConflictResponse] = Field(default_factory=list)


class MaintenanceRun(BaseModel):
    run_id: str
    dry_run: bool
    archived: int
    folded: int
    errors: list[str] = Field(default_factory=list)


class DeletedResponse(BaseModel):
    deleted: str


class ClearedFieldResponse(BaseModel):
    cleared: str


class ClearedResponse(BaseModel):
    cleared: bool


@router.get("", response_model=MemoryResponse)
async def get_memory(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> MemoryResponse:
    manager, scope = _manager_and_scope(request, caller)
    snapshot, page, conflicts = await asyncio.gather(
        asyncio.to_thread(manager.service(MemoryKind.PROFILE).snapshot, scope),  # type: ignore[attr-defined]
        asyncio.to_thread(
            manager.list_records,
            scope,
            kind=MemoryKind.SEMANTIC,
            include_history=False,
            limit=50,
        ),
        asyncio.to_thread(manager.list_conflicts, scope),
    )
    return MemoryResponse(
        schema_version=1,
        profile=_profile_response(snapshot),
        records=[_record_response(hit.record) for hit in page.items],
        conflicts=[_conflict_response(conflict) for conflict in conflicts],
    )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> ProfileResponse:
    manager, scope = _manager_and_scope(request, caller)
    snapshot = await _call(manager.service(MemoryKind.PROFILE).snapshot, scope)  # type: ignore[attr-defined]
    if snapshot is None:
        return ProfileResponse(schema_version=1, revision=0, fields={})
    return _profile_response(snapshot)  # type: ignore[return-value]


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    request: Request,
    body: ProfilePatchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
) -> ProfileResponse:
    manager, scope = _manager_and_scope(request, caller)
    for field_path in body.fields:
        if field_path not in PROFILE_FIELD_PATHS:
            raise InvalidMemoryProfile(f"unknown profile field: {field_path}")
    try:
        snapshot = await _call(
            manager.service(MemoryKind.PROFILE).update,  # type: ignore[attr-defined]
            scope,
            ProfilePatch(fields=body.fields, source="user_manual", evidence="ui:profile_patch"),
            expected_revision=body.expected_revision,
        )
    except Exception as exc:
        raise _wire_error(exc) from exc
    return _profile_response(snapshot)  # type: ignore[return-value]


@router.delete("/profile/{field_path:path}", response_model=ClearedFieldResponse)
async def clear_profile_field(
    request: Request,
    field_path: str,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
) -> ClearedFieldResponse:
    manager, scope = _manager_and_scope(request, caller)
    try:
        await _call(manager.clear_profile_field, scope, field_path)
    except Exception as exc:
        raise _wire_error(exc) from exc
    return ClearedFieldResponse(cleared=field_path)


@router.get("/search", response_model=MemorySearchPage)
async def search_memory(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    q: str = "",
    kinds: str | None = None,
    limit: int = 8,
    cursor: str | None = None,
) -> MemorySearchPage:
    manager, scope = _manager_and_scope(request, caller)
    selected = [part.strip() for part in kinds.split(",") if part.strip()] if kinds else [MemoryKind.SEMANTIC]
    if MemoryKind.PROFILE in selected:
        raise InvalidMemoryProfile("profile is not a search kind; read /memory/profile")
    try:
        return await _call(
            manager.search,
            scope,
            MemoryQuery(
                kinds=selected,
                text=q or None,
                limit=max(1, min(limit, 100)),
                cursor=cursor,
            ),
        )
    except Exception as exc:
        raise _wire_error(exc) from exc


@router.get("/items", response_model=RecordPage)
async def list_memory_items(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    kind: str = Query(...),
    status: str | None = None,
    include_history: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> RecordPage:
    manager, scope = _manager_and_scope(request, caller)
    if kind == MemoryKind.PROFILE:
        raise InvalidMemoryProfile("profile is returned by /memory/profile")
    filters = {"status": status} if status else {}
    try:
        page = await _call(
            manager.list_records,
            scope,
            kind=kind,
            filters=filters,
            include_history=include_history,
            limit=max(1, min(limit, 100)),
            cursor=cursor,
        )
    except Exception as exc:
        raise _wire_error(exc) from exc
    return RecordPage(
        items=[_record_response(hit.record) for hit in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/items/{kind}/{record_id}", response_model=MemoryRecordResponse)
async def get_memory_item(
    request: Request,
    kind: str,
    record_id: str,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> MemoryRecordResponse:
    manager, scope = _manager_and_scope(request, caller)
    record = await _call(manager.get_record, scope, kind, record_id)
    if record is None:
        raise MemoryNotFoundAPI(f"memory record {kind}/{record_id} not found")
    return _record_response(record)


@router.delete("/items/{kind}/{record_id}", response_model=DeletedResponse)
async def delete_memory_item(
    request: Request,
    kind: str,
    record_id: str,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
) -> DeletedResponse:
    manager, scope = _manager_and_scope(request, caller)
    try:
        await _call(manager.delete_record, scope, kind, record_id, mode="soft", reason="user_forget")
    except Exception as exc:
        raise _wire_error(exc) from exc
    return DeletedResponse(deleted=record_id)


@router.post("/items/{kind}/{record_id}/remember", response_model=MemoryRecordResponse)
async def remember_memory_item(
    request: Request,
    kind: str,
    record_id: str,
    body: RememberRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
) -> MemoryRecordResponse:
    manager, scope = _manager_and_scope(request, caller)
    if kind != MemoryKind.SEMANTIC:
        raise MemoryKindUnsupported("remember only applies to evictable fact kinds")
    try:
        record = await _call(manager.set_explicit, scope, kind, record_id, body.explicit)
    except Exception as exc:
        raise _wire_error(exc) from exc
    if record is None:
        raise MemoryNotFoundAPI(f"memory record {kind}/{record_id} not found")
    return _record_response(record)


@router.get("/facts", response_model=FactPage, deprecated=True)
async def list_facts(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    include_history: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> FactPage:
    manager, scope = _manager_and_scope(request, caller)
    page = await _call(
        manager.list_records,
        scope,
        kind=MemoryKind.SEMANTIC,
        include_history=include_history,
        limit=max(1, min(limit, 100)),
        cursor=cursor,
    )
    return FactPage(items=[_fact_response(hit.record) for hit in page.items], next_cursor=page.next_cursor)


@router.get("/conflicts", response_model=ConflictPage)
async def list_conflicts(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    include_resolved: bool = False,
) -> ConflictPage:
    manager, scope = _manager_and_scope(request, caller)
    conflicts = await _call(manager.list_conflicts, scope, include_resolved=include_resolved)
    return ConflictPage(items=[_conflict_response(conflict) for conflict in conflicts])


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictResponse)
async def resolve_conflict(
    request: Request,
    conflict_id: str,
    body: ConflictResolveRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
) -> ConflictResponse:
    manager, scope = _manager_and_scope(request, caller)
    if body.resolution == "merge" and not (body.merged_content or "").strip():
        raise MemoryRequestInvalid("merge requires merged_content")
    try:
        conflict = await _call(
            manager.resolve_conflict,
            scope,
            conflict_id,
            body.resolution,
            merged_content=body.merged_content,
        )
    except KeyError as exc:
        raise MemoryNotFoundAPI(f"conflict {conflict_id} not found") from exc
    except Exception as exc:
        raise _wire_error(exc) from exc
    return _conflict_response(conflict)


@router.get("/export", response_model=MemoryExport)
async def export_memory(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    kinds: str | None = None,
) -> MemoryExport:
    manager, scope = _manager_and_scope(request, caller)
    selected = [part.strip() for part in kinds.split(",") if part.strip()] if kinds else None
    try:
        exported = await _call(manager.export, scope, kinds=selected)
    except Exception as exc:
        raise _wire_error(exc) from exc
    return MemoryExport(
        schema_version=exported.schema_version,
        exported_at=exported.exported_at.isoformat(),
        profile=None if exported.profile is None else _profile_dict(exported.profile, 0),
        records=[_record_response(record) for record in exported.records],
        conflicts=[_conflict_response(conflict) for conflict in exported.conflicts],
        maintenance=exported.maintenance,
    )


@router.delete("", response_model=ClearedResponse)
async def clear_memory(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    kinds: str | None = None,
) -> ClearedResponse:
    manager, scope = _manager_and_scope(request, caller)
    selected = [part.strip() for part in kinds.split(",") if part.strip()] if kinds else None
    try:
        await _call(manager.clear, scope, kinds=selected)
    except Exception as exc:
        raise _wire_error(exc) from exc
    return ClearedResponse(cleared=True)


@router.post("/maintenance/run", response_model=MaintenanceRun)
async def run_maintenance(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
    dry_run: bool = True,
) -> MaintenanceRun:
    del caller
    manager = _manager(request)
    service = getattr(request.state, "memory_maintenance", None)
    if not isinstance(service, MemoryMaintenanceService):
        service = MemoryMaintenanceService(manager)
        request.state.memory_maintenance = service
    result = await asyncio.to_thread(service.run_once, dry_run=dry_run, force=True)
    return MaintenanceRun(
        run_id=result.run_id,
        dry_run=result.dry_run,
        archived=result.archived,
        folded=result.folded,
        errors=result.errors,
    )


def _manager(request: Request) -> MemoryManager:
    manager = getattr(request.state, "memory_manager", None)
    return manager if isinstance(manager, MemoryManager) else get_memory_manager()


def _manager_and_scope(request: Request, caller: RunCaller) -> tuple[MemoryManager, MemoryScope]:
    return _manager(request), MemoryScope(
        user_id=caller.user_id or DEFAULT_USER_ID,
        agent_name=None,
        thread_id=None,
    )


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (MemoryConflictError, MemoryKindNotSupported, MemoryCorruptionError, MemoryNotFoundError, ValueError) as exc:
        raise _wire_error(exc) from exc


def _wire_error(exc: Exception) -> Exception:
    if isinstance(exc, MemoryConflictError):
        return MemoryConflictAPI(str(exc))
    if isinstance(exc, MemoryKindNotSupported):
        return MemoryKindUnsupported(str(exc))
    if isinstance(exc, MemoryCorruptionError):
        return MemoryCorrupted()
    if isinstance(exc, (MemoryNotFoundError, KeyError)):
        return MemoryNotFoundAPI("memory record not found")
    if isinstance(exc, ValueError):
        return InvalidMemoryProfile(str(exc))
    return exc


def _profile_response(snapshot: ProfileSnapshot | None) -> ProfileResponse | None:
    if snapshot is None:
        return None
    return _profile_dict(snapshot.profile.model_dump(mode="json"), snapshot.revision)


def _profile_dict(profile: dict[str, Any], revision: int) -> ProfileResponse:
    fields: dict[str, Any] = {}
    for path in PROFILE_FIELD_PATHS:
        section, _, name = path.partition(".")
        if name:
            parent = profile.get(section)
            value = parent.get(name) if isinstance(parent, dict) else None
        else:
            value = profile.get(path)
        if value is not None:
            fields[path] = value
    return ProfileResponse(
        schema_version=int(profile.get("schema_version", 1)),
        revision=revision or int(profile.get("revision", 0)),
        fields=fields,
    )


def _record_response(record: MemoryRecord) -> MemoryRecordResponse:
    return MemoryRecordResponse(
        id=record.id,
        kind=record.kind,
        subtype=record.subtype,
        content=record.content,
        structured=record.structured,
        status=record.status,
        revision=record.revision,
        explicit_remember=record.explicit_remember,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _fact_response(record: MemoryRecord) -> FactResponse:
    structured = record.structured
    return FactResponse(
        id=record.id,
        content=record.content,
        category=str(structured.get("category") or "context"),
        topic_key=record.topic_key,
        confidence=record.confidence,
        source=record.source,
        status=record.status,
        explicit_remember=record.explicit_remember,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _conflict_response(conflict: MemoryConflict) -> ConflictResponse:
    return ConflictResponse(
        id=conflict.id,
        kind=conflict.kind,
        field_path=conflict.field_path,
        status=conflict.status,
    )
