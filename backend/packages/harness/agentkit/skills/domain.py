"""技能目录的共享领域记录。

这些 dataclass 是服务层、存储层和 HTTP 层之间的稳定边界：不暴露 ORM 行，
也不把 SQLAlchemy 或 Git 的具体实现泄漏给调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from agentkit.skills.parser import ParsedSkill
from agentkit.skills.types import Skill


class SkillScope(StrEnum):
    """技能内容的可见范围。"""

    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class StagedFile:
    """一个待写入草稿分支的技能文件。"""

    path: str
    content: bytes
    mode: str = "text"


@dataclass(frozen=True, slots=True)
class StagedFileInfo:
    """编辑器/审核界面需要的文件元信息，不包含正文。"""

    path: str
    size_bytes: int
    mode: str
    sha256: str


class InvalidSkillPackage(ValueError):
    """技能包不满足路径、大小或 frontmatter 约束。"""

    code: str = "invalid_skill_package"


class SkillConflict(RuntimeError):
    """唯一的业务约束被并发写入或重复数据破坏。"""

    code: str = "skill_conflict"


@dataclass(frozen=True, slots=True)
class PackageManifest:
    """一次构建后的不可变技能包摘要。

    files 按 POSIX 相对路径排序；package_sha256 由同一排序后的规范输入计算，
    保证相同内容在不同映射顺序下得到相同版本哈希。
    """

    files: tuple[StagedFile, ...]
    package_sha256: str
    skill_md_sha256: str
    parsed: ParsedSkill


@dataclass(frozen=True, slots=True)
class SkillRecord:
    id: str
    owner_user_id: str
    private_name: str
    public_name: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class SkillVersionRecord:
    id: str
    skill_id: str
    skill_version: str
    version_seq: int
    package_sha256: str
    skill_md_sha256: str
    metadata: dict[str, Any]
    draft_ref: str | None
    draft_commit: str | None
    skill_path: str
    index_status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SkillSubmissionRecord:
    id: str
    skill_id: str
    version_id: str
    submitted_commit: str
    owner_user_id: str
    status: str
    submitted_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    reject_reason: str | None
    public_release_id: str | None


@dataclass(frozen=True, slots=True)
class SkillReleaseRecord:
    id: str
    batch_id: str
    version_id: str
    skill_id: str
    skill_version: str
    git_repository: str
    git_commit: str
    skill_path: str
    public_name: str
    source_commit: str | None
    approved_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PublicActiveRecord:
    skill_id: str
    release_id: str
    activated_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PrivateActiveRecord:
    skill_id: str
    owner_user_id: str
    version_id: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VisibleSkill:
    skill: Skill
    scope: SkillScope
    version: SkillVersionRecord
    release: SkillReleaseRecord | None


@dataclass(frozen=True, slots=True)
class ReviewItem:
    submission_id: str
    skill_id: str
    name: str
    version: str
    owner_user_id: str
    submitted_at: datetime
    submitted_commit: str


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    item: ReviewItem
    files: list[StagedFileInfo]
    skill_md: str
    diff: str
    metadata: dict[str, Any]
    package_bytes: int


__all__ = [
    "InvalidSkillPackage",
    "PackageManifest",
    "PrivateActiveRecord",
    "PublicActiveRecord",
    "ReviewDetail",
    "ReviewItem",
    "SkillConflict",
    "SkillRecord",
    "SkillReleaseRecord",
    "SkillScope",
    "SkillSubmissionRecord",
    "SkillVersionRecord",
    "StagedFile",
    "StagedFileInfo",
    "VisibleSkill",
]
