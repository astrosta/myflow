"""Wire models for the user-isolated Git skill author API.

The author surface deliberately exposes container paths, never host cache paths.
Private details (owner id, scan findings and container_file) are only returned to
the owner.  There is no admin review/release wire model in v2.
"""

from __future__ import annotations

from typing import Any, Literal

from agentkit.skills.package import MAX_SKILL_PATH_CHARS
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentkit.skills.parser import SKILL_NAME_RE

_SKILL_NAME = SKILL_NAME_RE


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_SKILL_NAME, max_length=64)
    description: str = Field(min_length=1, max_length=2000)

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("description must not be blank")
        return text


class WriteFileRequest(BaseModel):
    """Text-file write into the private staging table."""

    model_config = ConfigDict(extra="forbid")

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


class SecurityFindingResponse(BaseModel):
    rule_id: str = ""
    title: str = ""
    severity: str = "SAFE"
    category: str = ""
    file_path: str | None = None
    line_number: int | None = None
    remediation: str | None = None


class SecurityScanResponse(BaseModel):
    scan_id: str
    skill_id: str
    version_id: str
    status: Literal["completed", "failed"]
    is_safe: bool
    max_severity: str
    findings_count: int
    findings: list[SecurityFindingResponse] = Field(default_factory=list)
    scanner: str
    scanner_version: str | None = None
    duration_ms: int
    error: str | None = None
    created_at: str


class SecurityScanSummary(BaseModel):
    scan_id: str
    version_id: str
    status: Literal["completed", "failed"]
    is_safe: bool
    max_severity: str
    findings_count: int
    scanner_version: str | None = None
    duration_ms: int = 0
    created_at: str


class SkillListItem(BaseModel):
    """One owner-visible catalog row with lifecycle and latest scan state."""

    id: str
    name: str
    description: str
    scope: Literal["private"]
    owner_user_id: str | None = None
    skill_version: str | None = None
    version_id: str | None = None
    git_commit: str | None = None
    skill_path: str | None = None
    status: Literal["draft", "unscanned", "scan_failed", "scanned", "active"]
    source: Literal["manual", "extracted"] = "manual"
    source_uri: str | None = None
    enabled: bool = True
    container_file: str | None = None
    index_status: str | None = None
    active_version_id: str | None = None
    active: bool = False
    latest_scan: SecurityScanSummary | None = None
    can_activate: bool = False
    activation_blocked_reason: str | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillListItem]


class DraftTreeResponse(BaseModel):
    files: list[DraftFileInfo]


class DraftView(BaseModel):
    skill: SkillListItem
    files: list[DraftFileInfo]
    latest_version: SkillVersionResponse | None = None
    latest_scan: SecurityScanResponse | None = None


class SaveResponse(BaseModel):
    version: SkillVersionResponse


class VersionListResponse(BaseModel):
    versions: list[SkillVersionResponse]


class ScanListResponse(BaseModel):
    scans: list[SecurityScanResponse]


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str | None = Field(default=None, max_length=64)


class ActivateResponse(BaseModel):
    version: SkillVersionResponse
    active_version_id: str
    active: bool = True
    index_status: str
    indexed: bool | None = None


__all__ = [
    "ActivateRequest",
    "ActivateResponse",
    "CreateDraftRequest",
    "DraftFileContentResponse",
    "DraftFileInfo",
    "DraftTreeResponse",
    "DraftView",
    "SaveResponse",
    "ScanListResponse",
    "SecurityFindingResponse",
    "SecurityScanResponse",
    "SecurityScanSummary",
    "SkillListItem",
    "SkillListResponse",
    "SkillVersionResponse",
    "VersionListResponse",
    "WriteFileRequest",
]
