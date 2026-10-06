"""Browser uploads into a thread sandbox."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, UploadFile

from backend.app.gateway.deps import RunServices, get_run_services, require_scope
from backend.app.gateway.errors import UploadInvalid
from backend.app.gateway.models.request import ThreadId
from backend.app.gateway.models.response import DeletedUploadResponse, UploadListResponse, UploadResponse
from backend.app.gateway.run_models import RunCaller
from backend.app.gateway.thread_access import require_visible_thread
from backend.app.gateway.thread_files import check_upload_filename, delete_upload_file, list_upload_dir, save_uploads

router = APIRouter(prefix="/threads", tags=["uploads"])

UploadFilename = Annotated[str, Path(min_length=1, max_length=255)]


@router.get("/{thread_id}/uploads/list", response_model=UploadListResponse)
async def list_uploads(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> UploadListResponse:
    await require_visible_thread(services, thread_id, caller)
    files = await list_upload_dir(thread_id, caller.user_id)
    return UploadListResponse(files=files, count=len(files))


@router.post("/{thread_id}/uploads", status_code=201, response_model=UploadResponse)
async def create_uploads(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> UploadResponse:
    await require_visible_thread(services, thread_id, caller)
    if not files:
        raise UploadInvalid("At least one file is required")
    saved = await save_uploads(thread_id, caller.user_id, files)
    return UploadResponse(files=saved)


@router.delete("/{thread_id}/uploads/{filename}", response_model=DeletedUploadResponse)
async def delete_upload(
    thread_id: ThreadId,
    filename: UploadFilename,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> DeletedUploadResponse:
    await require_visible_thread(services, thread_id, caller)
    check_upload_filename(filename)
    await delete_upload_file(thread_id, caller.user_id, filename)
    return DeletedUploadResponse(deleted=filename)
