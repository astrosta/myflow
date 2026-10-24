"""Wire models for the Git-backed skill author and admin review APIs.

The author surface deliberately exposes container paths, never host cache paths.
Private details (owner id and container_file) are only filled for the owner.
Review detail is supplied by SkillReviewService from the submitted Git commit.
"""

from __future__ import annotations

from typing import Any, Literal

from agentkit.skills.package import MAX_SKILL_PATH_CHARS
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentkit.skills.parser import SKILL_NAME_RE

# 请求模型和打包器共用同一条规则：API 拒绝的名字必须正好是 build_package 拒绝的名字。
_SKILL_NAME = SKILL_NAME_RE


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_SKILL_NAME, max_length=64)
    description: str = Field(min_length=1, max_length=2000)


class WriteFileRequest(BaseModel):
    """Text-file write into the private staging table.

    The content field is UTF-8 text; binary attachments go through the
    multipart upload route so a JSON string is never mistaken for bytes.
    """

    model_config = ConfigDict(extra="forbid")

    # 和 agentkit.skills.package.MAX_SKILL_PATH_CHARS 同一个上限：写进来的路径
    # 必须能通过包校验，也不该撑爆 skill_draft_files 的 (skill_id, path) 主键。
    path: str = Field(min_length=1, max_length=MAX_SKILL_PATH_CHARS)
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
    reject_reason: str | None = None


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
    reject_reason: str | None = None


class VersionListResponse(BaseModel):
    versions: list[SkillVersionResponse]


class RejectRequest(BaseModel):
    """Admin rejection reason; whitespace alone is not a reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class ReviewSummary(BaseModel):
    submission_id: str
    skill_id: str
    name: str
    version: str
    owner_user_id: str
    submitted_at: str
    submitted_commit: str


class ReviewDetailResponse(ReviewSummary):
    files: list[DraftFileInfo]
    skill_md: str
    diff: str
    metadata: dict[str, Any]
    package_bytes: int


class ReviewQueueResponse(BaseModel):
    items: list[ReviewSummary]


class ReleaseResponse(BaseModel):
    release_id: str
    skill_id: str
    skill_version: str
    git_repository: str
    git_commit: str
    skill_path: str
    public_name: str
    source_commit: str | None = None
    approved_by: str
    created_at: str


class UnpublishResponse(BaseModel):
    skill_id: str
    unpublished: bool = True


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1, max_length=128)


__all__ = [
    "ActivateRequest",
    "CreateDraftRequest",
    "DraftFileContentResponse",
    "DraftFileInfo",
    "DraftTreeResponse",
    "DraftView",
    "RejectRequest",
    "ReleaseResponse",
    "SKILL_NAME_RE",
    "ReviewDetailResponse",
    "ReviewQueueResponse",
    "ReviewSummary",
    "SaveResponse",
    "SkillListItem",
    "SkillListResponse",
    "SkillVersionResponse",
    "SubmissionResponse",
    "UnpublishResponse",
    "VersionListResponse",
    "WriteFileRequest",
]
