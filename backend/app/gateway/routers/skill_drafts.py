"""Author-facing HTTP API for user-isolated skill drafts, scans and activation.

Every write route requires skills:write; author reads require skills:read.
A foreign owner is always indistinguishable from an absent skill (404).  The
responses contain sandbox container paths only, never host cache paths.

Lifecycle endpoints:
    POST /skills/{id}/draft/save   -> inert draft version
    POST /skills/{id}/scan         -> cisco-ai-skill-scanner result persisted
    POST /skills/{id}/activate     -> latest version + passing scan -> active
There is no submit/withdraw/admin-review route in v2.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import queue
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, File, Path, Query, Request, Response, UploadFile
from fastapi import status as http_status
from fastapi.responses import StreamingResponse

from agentkit.config import get_app_config
from agentkit.config.skills_config import DEFAULT_SKILLS_CONTAINER_PATH
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SecurityScanRecord,
    SkillConflict,
    SkillRecord,
    SkillScanUnavailable,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.package import MAX_SKILL_PATH_CHARS, validate_skill_path
from agentkit.skills.parser import SkillParseError, parse_skill_text
from backend.app.gateway import skills_catalog
from backend.app.gateway.deps import get_skill_author_service, require_scope
from backend.app.gateway.errors import (
    SkillConflictAPI,
    SkillNotFound,
    SkillPackageInvalid,
    SkillScannerUnavailable,
)
from backend.app.gateway.models.skills import (
    ActivateRequest,
    ActivateResponse,
    CreateDraftRequest,
    DraftFileContentResponse,
    DraftFileInfo,
    DraftTreeResponse,
    DraftView,
    SaveResponse,
    ScanListResponse,
    SecurityFindingResponse,
    SecurityScanResponse,
    SecurityScanSummary,
    SkillListItem,
    SkillListResponse,
    SkillVersionResponse,
    VersionListResponse,
    WriteFileRequest,
)
from backend.app.gateway.run_models import RunCaller

logger = logging.getLogger(__name__)


async def _drain_index_outbox() -> bool | None:
    """Best-effort immediate indexing; None means this deployment has no projection."""
    runtime = skills_catalog.get_skill_runtime()
    outbox = getattr(runtime, "outbox", None)
    if outbox is None:
        return None
    try:
        await outbox.run_once()
    except Exception as exc:  # noqa: BLE001 - the durable outbox worker will retry
        logger.warning("Immediate skill indexing failed: %s", exc)
        return False
    return True


router = APIRouter(prefix="/skills", tags=["skill-drafts"])

SkillId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
SkillPath = Annotated[str, Query(min_length=1, max_length=MAX_SKILL_PATH_CHARS)]
AuthorService = Annotated[Any, Depends(get_skill_author_service)]
Writer = Annotated[RunCaller, Depends(require_scope("skills:write"))]
Reader = Annotated[RunCaller, Depends(require_scope("skills:read"))]

T = TypeVar("T")
_ABSENT = "skill not found"


async def _invoke(action: Awaitable[T]) -> T:
    """Translate domain errors at the HTTP boundary; details never include paths."""
    try:
        return await action
    except InvalidSkillPackage as exc:
        raise SkillPackageInvalid(str(exc)) from exc
    except SkillScanUnavailable as exc:
        raise SkillScannerUnavailable(str(exc)) from exc
    except SkillConflict as exc:
        raise SkillConflictAPI(str(exc)) from exc


async def _owned(
    service: Any,
    caller: RunCaller,
    skill_id: str,
    *,
    include_deleted: bool = False,
) -> SkillRecord:
    """Return the caller's skill or raise the same 404 an absent skill gets."""
    skill = await service.get_owned_skill(
        owner_user_id=caller.user_id or "",
        skill_id=skill_id,
        include_deleted=include_deleted,
    )
    if skill is None:
        raise SkillNotFound(_ABSENT)
    return skill


def _valid_path(path: str) -> str:
    """Reject traversal before any service call, with the coded 422 body."""
    try:
        return validate_skill_path(path)
    except InvalidSkillPackage as exc:
        raise SkillPackageInvalid(str(exc)) from exc


def _container_root() -> str:
    """Configured container root; the default is only a last resort without config."""
    try:
        configured = get_app_config().skills.container_path
    except Exception:  # noqa: BLE001 - routes are testable without a config.yaml
        return DEFAULT_SKILLS_CONTAINER_PATH
    return configured.rstrip("/") or "/"


def _container_file(name: str) -> str:
    return f"{_container_root()}/private/{name}/SKILL.md"


def _effective_index_status(status: str | None) -> str | None:
    """Pending without an outbox worker would never settle; report it as disabled."""
    if status == "pending" and not skills_catalog.index_projection_configured():
        return "disabled"
    return status


def _version(view: SkillVersionRecord) -> SkillVersionResponse:
    return SkillVersionResponse(
        version_id=view.id,
        skill_version=view.skill_version,
        package_sha256=view.package_sha256,
        skill_md_sha256=view.skill_md_sha256,
        draft_ref=view.draft_ref,
        draft_commit=view.draft_commit,
        skill_path=view.skill_path,
        index_status=_effective_index_status(view.index_status) or "unindexed",
        created_at=view.created_at.isoformat(),
    )


def _file_info(view: StagedFileInfo) -> DraftFileInfo:
    return DraftFileInfo(
        path=view.path,
        size_bytes=view.size_bytes,
        mode=view.mode,
        sha256=view.sha256,
    )


def _finding(item: dict[str, Any]) -> SecurityFindingResponse:
    return SecurityFindingResponse(**item)


def _scan_response(scan: SecurityScanRecord) -> SecurityScanResponse:
    return SecurityScanResponse(
        scan_id=scan.id,
        skill_id=scan.skill_id,
        version_id=scan.version_id,
        status=scan.status,  # type: ignore[arg-type]
        is_safe=scan.is_safe,
        max_severity=scan.max_severity,
        findings_count=scan.findings_count,
        findings=[_finding(item) for item in scan.findings],
        scanner=scan.scanner,
        scanner_version=scan.scanner_version,
        duration_ms=scan.duration_ms,
        error=scan.error,
        created_at=scan.created_at.isoformat(),
    )


def _scan_summary(scan: SecurityScanRecord) -> SecurityScanSummary:
    return SecurityScanSummary(
        scan_id=scan.id,
        version_id=scan.version_id,
        status=scan.status,  # type: ignore[arg-type]
        is_safe=scan.is_safe,
        max_severity=scan.max_severity,
        findings_count=scan.findings_count,
        scanner_version=scan.scanner_version,
        duration_ms=scan.duration_ms,
        created_at=scan.created_at.isoformat(),
    )


def _file_info_from_staged(file: StagedFile) -> DraftFileInfo:
    return DraftFileInfo(
        path=file.path,
        size_bytes=len(file.content),
        mode=file.mode,
        sha256=hashlib.sha256(file.content).hexdigest(),
    )


def _name_and_description_from_skill_md(file: StagedFile | None) -> tuple[str, str]:
    """Read the visible identity from SKILL.md, never from the internal row name."""
    if file is None:
        return "", ""
    try:
        parsed = parse_skill_text(file.content.decode("utf-8"))
    except (UnicodeDecodeError, SkillParseError, ValueError):
        return "", ""
    return parsed.name, parsed.description


def _lifecycle(
    latest: SkillVersionRecord | None,
    active: SkillVersionRecord | None,
    scan: SecurityScanRecord | None,
) -> tuple[str, bool, str | None]:
    """Derive the owner-facing lifecycle from immutable version/scan state."""
    if latest is None:
        return "draft", False, "请先保存技能"
    if active is not None and active.id == latest.id:
        return "active", False, "当前版本已生效"
    if scan is None:
        return "unscanned", False, "请先完成安全扫描"
    if scan.status != "completed":
        return "scan_failed", False, "安全扫描执行失败，请重新扫描"
    if not scan.is_safe:
        return "scan_failed", False, "安全扫描未通过，请修复发现的问题后重新扫描"
    return "scanned", True, None


async def _list_item(
    service: Any,
    caller: RunCaller,
    record: SkillRecord,
    *,
    latest: SkillVersionRecord | None = None,
    active: SkillVersionRecord | None = None,
    scan: SecurityScanRecord | None = None,
) -> SkillListItem:
    owner = caller.user_id or ""
    if latest is None:
        latest = await _invoke(
            service.latest_version(owner_user_id=owner, skill_id=record.id)
        )
    if active is None:
        active = await _invoke(
            service.active_version(owner_user_id=owner, skill_id=record.id)
        )
    if scan is None and latest is not None:
        scan = await _invoke(
            service.latest_scan(
                owner_user_id=owner, skill_id=record.id, version_id=latest.id
            )
        )

    name = record.private_name
    description = ""
    if latest is not None:
        name = str(latest.metadata.get("name") or name)
        description = str(latest.metadata.get("description") or "")
    if latest is None or not description:
        staged = await service.read_file(
            owner_user_id=owner, skill_id=record.id, path="SKILL.md"
        )
        staged_name, staged_description = _name_and_description_from_skill_md(staged)
        if latest is None and staged_name:
            name = staged_name
        description = description or staged_description

    status, can_activate, blocked = _lifecycle(latest, active, scan)
    return SkillListItem(
        id=record.id,
        name=name,
        description=description,
        scope="private",
        owner_user_id=record.owner_user_id,
        skill_version=latest.skill_version if latest is not None else None,
        version_id=latest.id if latest is not None else None,
        git_commit=latest.draft_commit if latest is not None else None,
        skill_path=latest.skill_path if latest is not None else None,
        status=status,  # type: ignore[arg-type]
        source=record.source,  # type: ignore[arg-type]
        source_uri=record.source_uri,
        enabled=bool(record.enabled),
        container_file=(
            _container_file(record.private_name)
            if skills_catalog._private_mounts_supported()
            else None
        ),
        index_status=_effective_index_status(
            active.index_status
            if active is not None
            else (latest.index_status if latest is not None else None)
        ),
        active_version_id=active.id if active is not None else None,
        active=active is not None and latest is not None and active.id == latest.id,
        latest_scan=_scan_summary(scan) if scan is not None else None,
        can_activate=can_activate,
        activation_blocked_reason=blocked,
    )


# /skills/mine must stay before /skills/{skill_id} routes.
@router.get("/mine", response_model=SkillListResponse)
async def list_my_skills(caller: Reader, service: AuthorService) -> SkillListResponse:
    owner = caller.user_id or ""
    records = await _invoke(service.list_mine(owner_user_id=owner))
    skills: list[SkillListItem] = []
    for record in records:
        versions = await _invoke(
            service.list_versions(owner_user_id=owner, skill_id=record.id)
        )
        latest = versions[-1] if versions else None
        active = await _invoke(
            service.active_version(owner_user_id=owner, skill_id=record.id)
        )
        scan = None
        if latest is not None:
            scan = await _invoke(
                service.latest_scan(
                    owner_user_id=owner, skill_id=record.id, version_id=latest.id
                )
            )
        skills.append(
            await _list_item(
                service,
                caller,
                record,
                latest=latest,
                active=active,
                scan=scan,
            )
        )
    return SkillListResponse(skills=skills)


@router.post("", response_model=SkillListItem, status_code=http_status.HTTP_201_CREATED)
async def create_skill(
    body: CreateDraftRequest,
    caller: Writer,
    service: AuthorService,
) -> SkillListItem:
    record = await _invoke(
        service.create_draft(
            owner_user_id=caller.user_id or "",
            name=body.name,
            description=body.description,
        )
    )
    return SkillListItem(
        id=record.id,
        name=record.private_name,
        description=body.description,
        scope="private",
        owner_user_id=record.owner_user_id,
        status="draft",
        source=record.source,  # type: ignore[arg-type]
        source_uri=record.source_uri,
        enabled=bool(record.enabled),
        container_file=(
            _container_file(record.private_name)
            if skills_catalog._private_mounts_supported()
            else None
        ),
        can_activate=False,
        activation_blocked_reason="请先保存技能",
    )


@router.post("/{skill_id}/draft/open", response_model=DraftTreeResponse)
async def open_draft(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> DraftTreeResponse:
    await _owned(service, caller, skill_id, include_deleted=True)
    files = await _invoke(
        service.open_draft(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    return DraftTreeResponse(files=[_file_info(file) for file in files])


@router.get("/{skill_id}/draft", response_model=DraftView)
async def get_draft(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> DraftView:
    record = await _owned(service, caller, skill_id)
    files = await _invoke(
        service.list_files(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    latest = await _invoke(
        service.latest_version(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    scan = None
    if latest is not None:
        scan = await _invoke(
            service.latest_scan(
                owner_user_id=caller.user_id or "",
                skill_id=skill_id,
                version_id=latest.id,
            )
        )
    item = await _list_item(service, caller, record, latest=latest, scan=scan)
    return DraftView(
        skill=item,
        files=[_file_info(file) for file in files],
        latest_version=_version(latest) if latest is not None else None,
        latest_scan=_scan_response(scan) if scan is not None else None,
    )


@router.get("/{skill_id}/draft/tree", response_model=DraftTreeResponse)
async def list_draft_tree(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> DraftTreeResponse:
    await _owned(service, caller, skill_id)
    files = await _invoke(
        service.list_files(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    return DraftTreeResponse(files=[_file_info(file) for file in files])


@router.get("/{skill_id}/draft/file", response_model=DraftFileContentResponse)
async def read_draft_file(
    skill_id: SkillId,
    path: SkillPath,
    caller: Writer,
    service: AuthorService,
) -> DraftFileContentResponse:
    await _owned(service, caller, skill_id)
    _valid_path(path)
    file = await _invoke(
        service.read_file(
            owner_user_id=caller.user_id or "", skill_id=skill_id, path=path
        )
    )
    if file is None:
        raise SkillNotFound(f"draft file {path!r} not found")
    try:
        content = file.content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(file.content).decode("ascii")
        encoding = "base64"
    return DraftFileContentResponse(
        path=file.path,
        size_bytes=len(file.content),
        mode=file.mode,
        sha256=_file_info_from_staged(file).sha256,
        content=content,
        encoding=encoding,
    )


@router.put("/{skill_id}/draft/file", response_model=DraftFileInfo)
async def write_draft_file(
    skill_id: SkillId,
    body: WriteFileRequest,
    caller: Writer,
    service: AuthorService,
) -> DraftFileInfo:
    await _owned(service, caller, skill_id)
    _valid_path(body.path)
    info = await _invoke(
        service.put_file(
            owner_user_id=caller.user_id or "",
            skill_id=skill_id,
            path=body.path,
            content=body.content.encode("utf-8"),
            mode=body.mode,
        )
    )
    return _file_info(info)


@router.post("/{skill_id}/draft/files", response_model=DraftTreeResponse)
async def upload_draft_files(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> DraftTreeResponse:
    await _owned(service, caller, skill_id)
    uploads = list(files or [])
    if not uploads:
        raise SkillPackageInvalid("at least one file is required")

    # Validate every name before staging anything; one traversal must not
    # leave the earlier attachments half-written.
    for upload in uploads:
        _valid_path(upload.filename or "")

    for upload in uploads:
        content = await upload.read()
        await _invoke(
            service.put_file(
                owner_user_id=caller.user_id or "",
                skill_id=skill_id,
                path=upload.filename or "",
                content=content,
                mode="text",
            )
        )
    staged = await _invoke(
        service.list_files(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    return DraftTreeResponse(files=[_file_info(file) for file in staged])


@router.delete("/{skill_id}/draft/file", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_draft_file(
    skill_id: SkillId,
    path: SkillPath,
    caller: Writer,
    service: AuthorService,
) -> Response:
    await _owned(service, caller, skill_id)
    _valid_path(path)
    await _invoke(
        service.delete_file(
            owner_user_id=caller.user_id or "", skill_id=skill_id, path=path
        )
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/{skill_id}/draft/save", response_model=SaveResponse)
async def save_draft(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> SaveResponse:
    await _owned(service, caller, skill_id)
    version = await _invoke(
        service.save(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    # Saving is inert: no runtime snapshot or Milvus row changes until activation.
    # 提交缓存清理是事件驱动的：这一版之后的旧版本不再需要本地缓存。
    await skills_catalog.forget_commits(skill_id)
    return SaveResponse(version=_version(version))


@router.get("/{skill_id}/versions", response_model=VersionListResponse)
async def list_versions(
    skill_id: SkillId,
    caller: Reader,
    service: AuthorService,
) -> VersionListResponse:
    await _owned(service, caller, skill_id)
    versions = await _invoke(
        service.list_versions(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    return VersionListResponse(versions=[_version(version) for version in versions])


def _wants_scan_stream(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


def _sse(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


def _scan_stream(
    service: AuthorService,
    caller: RunCaller,
    skill_id: str,
) -> StreamingResponse:
    """Push the scan phases, then the same JSON body the non-stream route returns."""

    async def events() -> AsyncIterator[str]:
        # queue.Queue, not asyncio.Queue: the scanner thread must enqueue a
        # status before scan() returns, or the sentinel can overtake it.
        statuses: queue.Queue[str | None] = queue.Queue()

        def on_status(message: str) -> None:
            text = str(message).strip()[:200]
            if text:
                statuses.put(text)

        async def work() -> SecurityScanRecord:
            try:
                return await service.scan_security(
                    owner_user_id=caller.user_id or "",
                    skill_id=skill_id,
                    on_status=on_status,
                )
            finally:
                statuses.put(None)

        logger.info("Security scan started for skill %s", skill_id)
        task = asyncio.create_task(work())
        try:
            while True:
                message = await asyncio.to_thread(statuses.get)
                if message is None:
                    break
                yield _sse("status", {"message": message})
            scan = await task
        except SkillScanUnavailable as exc:
            yield _sse(
                "error",
                {"status": 503, "code": "skill_scanner_unavailable", "detail": str(exc)},
            )
            return
        except SkillConflict as exc:
            yield _sse("error", {"status": 409, "code": "skill_conflict", "detail": str(exc)})
            return
        except InvalidSkillPackage as exc:
            yield _sse(
                "error",
                {"status": 422, "code": "invalid_skill_package", "detail": str(exc)},
            )
            return
        except Exception:
            logger.exception("Security scan failed for skill %s", skill_id)
            yield _sse(
                "error",
                {"status": 500, "code": "internal_error", "detail": "Internal server error"},
            )
            return
        logger.info(
            "Security scan finished for skill %s status=%s duration_ms=%s",
            skill_id,
            scan.status,
            scan.duration_ms,
        )
        yield _sse("result", _scan_response(scan).model_dump(mode="json"))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{skill_id}/scan", response_model=SecurityScanResponse)
async def scan_skill(
    skill_id: SkillId,
    request: Request,
    caller: Writer,
    service: AuthorService,
) -> SecurityScanResponse | StreamingResponse:
    await _owned(service, caller, skill_id)
    if _wants_scan_stream(request):
        return _scan_stream(service, caller, skill_id)
    logger.info("Security scan started for skill %s", skill_id)
    scan = await _invoke(
        service.scan_security(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    logger.info(
        "Security scan finished for skill %s status=%s duration_ms=%s",
        skill_id,
        scan.status,
        scan.duration_ms,
    )
    return _scan_response(scan)


@router.get("/{skill_id}/scans", response_model=ScanListResponse)
async def list_scans(
    skill_id: SkillId,
    caller: Reader,
    service: AuthorService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ScanListResponse:
    await _owned(service, caller, skill_id)
    scans = await _invoke(
        service.list_scans(
            owner_user_id=caller.user_id or "", skill_id=skill_id, limit=limit
        )
    )
    return ScanListResponse(scans=[_scan_response(scan) for scan in scans])


@router.post("/{skill_id}/activate", response_model=ActivateResponse)
async def activate_skill(
    skill_id: SkillId,
    body: ActivateRequest,
    caller: Writer,
    service: AuthorService,
) -> ActivateResponse:
    await _owned(service, caller, skill_id)
    version = await _invoke(
        service.activate(
            owner_user_id=caller.user_id or "",
            skill_id=skill_id,
            version_id=body.version_id,
            index_enabled=skills_catalog.index_projection_configured(),
        )
    )
    await skills_catalog.refresh_skill_runtime()

    # Build the vector row now when a projection is configured; the durable
    # outbox event is already committed, so an immediate failure is retryable.
    drained = await _drain_index_outbox()
    active = await _invoke(
        service.active_version(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    current = active or version
    indexed: bool | None = None if drained is None else drained
    if indexed is not None and current.index_status == "ready":
        indexed = True
    return ActivateResponse(
        version=_version(current),
        active_version_id=current.id,
        active=True,
        index_status=_effective_index_status(current.index_status) or "unindexed",
        indexed=indexed,
    )


@router.delete("/{skill_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> Response:
    await _owned(service, caller, skill_id)
    await _invoke(
        service.delete_draft(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    # 技能被删：它名下的提交缓存不再被任何活指针引用，就地收掉。
    await skills_catalog.forget_commits(skill_id)
    await skills_catalog.refresh_skill_runtime()
    # The delete outbox row is durable; drain it now so a retired skill does not
    # stay retrievable from Milvus until the next poll.
    await _drain_index_outbox()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
