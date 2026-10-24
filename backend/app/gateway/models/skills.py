"""Wire models for the Git-backed skill author API (M11 task 8).

The author surface deliberately exposes container paths, never host cache paths.
Private details (owner id and container_file) are only filled for the owner.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_SKILL_NAME = r"^[A-Za-z0-9_-]{1,64}$"


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_SKILL_NAME)
    description: str = Field(min_length=1, max_length=2000)


class WriteFileRequest(BaseModel):
    """Text-file write into the private staging table.

    The content field is UTF-8 text; binary attachments go through the
    multipart upload route so a JSON string is never mistaken for bytes.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    content: str
    mode: Literal["text", "executable"] = "text"


class DraftFileInfo(BaseModel):
    path: str
    size_bytes: int
    mode: str
    sha256: str


class DraftFileContentResponse(DraftFileInfo):
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class SkillVersionResponse(BaseModel):
    version_id: str
    skill_version: str
    package_sha256: str
    skill_md_sha256: str
    draft_ref: str | None = None
    draft_commit: str | None = None
    skill_path: str | None = None
    index_status: str
    created_at: str


class SkillListItem(BaseModel):
    """A public or private catalog row.

    The id field is the catalog skill id (the brief's exact field name).  The
    container_file path is a sandbox path and is only present for the owner of
    a private draft.
    """

    id: str
    name: str
    description: str
    scope: Literal["public", "private"]
    owner_user_id: str | None = None
    skill_version: str | None = None
    version_id: str | None = None
    git_commit: str | None = None
    skill_path: str | None = None
    status: str
    enabled: bool = True
    global_enabled: bool | None = None
    container_file: str | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillListItem]


class DraftTreeResponse(BaseModel):
    files: list[DraftFileInfo]


class DraftView(BaseModel):
    skill: SkillListItem
    files: list[DraftFileInfo]
    pending: bool = False


class SaveResponse(BaseModel):
    version: SkillVersionResponse


class SubmissionResponse(BaseModel):
    submission_id: str
    skill_id: str
    version_id: str
    status: str
    submitted_at: str
    owner_user_id: str


class VersionListResponse(BaseModel):
    versions: list[SkillVersionResponse]


__all__ = [
    "CreateDraftRequest",
    "DraftFileContentResponse",
    "DraftFileInfo",
    "DraftTreeResponse",
    "DraftView",
    "SaveResponse",
    "SkillListItem",
    "SkillListResponse",
    "SkillVersionResponse",
    "SubmissionResponse",
    "VersionListResponse",
    "WriteFileRequest",
]
