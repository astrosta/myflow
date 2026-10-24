"""Author-facing HTTP API for Git-backed private skill drafts (M11 task 8).

Every write route requires skills:write; author reads (/skills/mine and
/skills/{id}/versions) require skills:read.
A foreign owner is always indistinguishable from an absent skill (404).  The
responses contain sandbox container paths only, never host cache paths.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, File, Path, Query, Response, UploadFile
from fastapi import status as http_status

from agentkit.config import get_app_config
from agentkit.config.skills_config import DEFAULT_SKILLS_CONTAINER_PATH
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    SkillRecord,
    SkillSubmissionRecord,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.package import MAX_SKILL_PATH_CHARS, validate_skill_path
from agentkit.skills.parser import SkillParseError, parse_skill_text
from backend.app.gateway import skills_catalog
from backend.app.gateway.deps import get_skill_author_service, require_scope
from backend.app.gateway.errors import SkillConflictAPI, SkillNotFound, SkillPackageInvalid
from backend.app.gateway.models.skills import (
    CreateDraftRequest,
    DraftFileContentResponse,
    DraftFileInfo,
    DraftTreeResponse,
    DraftView,
    SaveResponse,
    SkillListItem,
    SkillListResponse,
    SkillVersionResponse,
    SubmissionResponse,
    VersionListResponse,
    WriteFileRequest,
)
from backend.app.gateway.run_models import RunCaller

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
    """Configured container root; the default is only a last resort when no config is readable."""
    try:
        configured = get_app_config().skills.container_path
    except Exception:  # noqa: BLE001 - routes are testable without a config.yaml
        return DEFAULT_SKILLS_CONTAINER_PATH
    return configured.rstrip("/") or "/"


def _container_file(name: str) -> str:
    """Private skills live under the configured container root, not a hardcoded one."""
    return f"{_container_root()}/private/{name}/SKILL.md"


def _version(view: SkillVersionRecord) -> SkillVersionResponse:
    return SkillVersionResponse(
        version_id=view.id,
        skill_version=view.skill_version,
        package_sha256=view.package_sha256,
        skill_md_sha256=view.skill_md_sha256,
        draft_ref=view.draft_ref,
        draft_commit=view.draft_commit,
        skill_path=view.skill_path,
        index_status=view.index_status,
        created_at=view.created_at.isoformat(),
    )


def _file_info(view: StagedFileInfo) -> DraftFileInfo:
    return DraftFileInfo(
        path=view.path,
        size_bytes=view.size_bytes,
        mode=view.mode,
        sha256=view.sha256,
    )


def _submission(view: SkillSubmissionRecord) -> SubmissionResponse:
    return SubmissionResponse(
        submission_id=view.id,
        skill_id=view.skill_id,
        version_id=view.version_id,
        status=view.status,
        submitted_at=view.submitted_at.isoformat(),
        owner_user_id=view.owner_user_id,
        reject_reason=view.reject_reason,
    )


def _file_info_from_staged(file: StagedFile) -> DraftFileInfo:
    return DraftFileInfo(
        path=file.path,
        size_bytes=len(file.content),
        mode=file.mode,
        sha256=hashlib.sha256(file.content).hexdigest(),
    )


def _description_from_skill_md(file: StagedFile | None) -> str:
    if file is None:
        return ""
    try:
        return parse_skill_text(file.content.decode("utf-8")).description
    except (UnicodeDecodeError, SkillParseError, ValueError):
        return ""


async def _list_item(
    service: Any,
    caller: RunCaller,
    record: SkillRecord,
    *,
    latest: SkillVersionRecord | None,
    submission: SkillSubmissionRecord | None,
) -> SkillListItem:
    description = ""
    if latest is not None:
        description = str(latest.metadata.get("description") or "")
    if not description:
        staged = await service.read_file(
            owner_user_id=caller.user_id or "",
            skill_id=record.id,
            path="SKILL.md",
        )
        description = _description_from_skill_md(staged)
    name = record.private_name
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
        status=submission.status if submission is not None else "draft",
        enabled=True,
        global_enabled=None,
        container_file=(
            _container_file(name) if skills_catalog._private_mounts_supported() else None
        ),
        reject_reason=(
            submission.reject_reason
            if submission is not None and record.owner_user_id == caller.user_id
            else None
        ),
    )


# /skills/mine must stay before /skills/{skill_id} routes.
@router.get("/mine", response_model=SkillListResponse)
async def list_my_skills(
    caller: Reader,
    service: AuthorService,
) -> SkillListResponse:
    records = await _invoke(service.list_mine(owner_user_id=caller.user_id or ""))
    skills: list[SkillListItem] = []
    for record in records:
        versions = await _invoke(
            service.list_versions(owner_user_id=caller.user_id or "", skill_id=record.id)
        )
        submission = await _invoke(
            service.latest_submission(
                owner_user_id=caller.user_id or "", skill_id=record.id
            )
        )
        skills.append(
            await _list_item(
                service,
                caller,
                record,
                latest=versions[-1] if versions else None,
                submission=submission,
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
        enabled=True,
        global_enabled=None,
        container_file=(
            _container_file(record.private_name)
            if skills_catalog._private_mounts_supported()
            else None
        ),
    )


@router.post("/{skill_id}/draft/open", response_model=DraftTreeResponse)
async def open_draft(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> DraftTreeResponse:
    # 作者删掉私有草稿后公共版本仍在：编辑入口必须能重新打开并恢复记录。
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
    versions = await _invoke(
        service.list_versions(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    submission = await _invoke(
        service.latest_submission(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    item = await _list_item(
        service,
        caller,
        record,
        latest=versions[-1] if versions else None,
        submission=submission,
    )
    return DraftView(
        skill=item,
        files=[_file_info(file) for file in files],
        pending=submission is not None and submission.status == "pending",
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
    await skills_catalog.refresh_skill_runtime()
    return SaveResponse(version=_version(version))


@router.post("/{skill_id}/submit", response_model=SubmissionResponse)
async def submit_skill(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> SubmissionResponse:
    await _owned(service, caller, skill_id)
    submission = await _invoke(
        service.submit(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    # Pending state is read from PostgreSQL by the routes, so the snapshot and
    # mount trees do not change here.  A full refresh would be pure overhead.
    return _submission(submission)


@router.post("/{skill_id}/withdraw", response_model=SubmissionResponse)
async def withdraw_skill(
    skill_id: SkillId,
    caller: Writer,
    service: AuthorService,
) -> SubmissionResponse:
    await _owned(service, caller, skill_id)
    submission = await _invoke(
        service.withdraw(owner_user_id=caller.user_id or "", skill_id=skill_id)
    )
    # Same as submit: only pending state changes, and that lives in PostgreSQL.
    return _submission(submission)


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
    await skills_catalog.refresh_skill_runtime()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
