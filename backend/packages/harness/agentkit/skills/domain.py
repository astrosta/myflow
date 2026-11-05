"""技能目录的共享领域记录。

这些 dataclass 是服务层、存储层和 HTTP 层之间的稳定边界：不暴露 ORM 行，
也不把 SQLAlchemy 或 Git 的具体实现泄漏给调用方。

v2 只支持用户私有技能：没有公共发布、审核提交或公共激活指针。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from agentkit.skills.parser import ParsedSkill
from agentkit.skills.types import Skill


class SkillScope(StrEnum):
    """技能内容的可见范围。

    PRIVATE 是 Git 目录里唯一的用户技能范围。BUILTIN 是随进程发布的
    只读技能，不属于任何用户；它从不经过 Git/数据库创作流程。
    """

    PRIVATE = "private"
    BUILTIN = "builtin"


@dataclass(frozen=True, slots=True)
class StagedFile:
    """一个待写入草稿分支的技能文件。"""

    path: str
    content: bytes
    mode: str = "text"


@dataclass(frozen=True, slots=True)
class StagedFileInfo:
    """编辑器界面需要的文件元信息，不包含正文。"""

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


class SkillScanUnavailable(RuntimeError):
    """安全扫描器没有安装或无法启动。"""

    code: str = "skill_scanner_unavailable"


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
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    source: str = "manual"
    source_uri: str | None = None
    enabled: bool = True
    load_count: int = 0


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
class PrivateActiveRecord:
    skill_id: str
    owner_user_id: str
    version_id: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SecurityScanRecord:
    """一次安全扫描的不可变结果，绑定一个已保存版本。"""

    id: str
    skill_id: str
    version_id: str
    owner_user_id: str
    package_sha256: str
    status: str
    is_safe: bool
    max_severity: str
    findings_count: int
    findings: list[dict[str, Any]]
    scanner: str
    scanner_version: str | None
    duration_ms: int
    error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VisibleSkill:
    """一个用户 active 技能的只读投影行。"""

    skill: Skill
    scope: SkillScope
    version: SkillVersionRecord


__all__ = [
    "InvalidSkillPackage",
    "PackageManifest",
    "PrivateActiveRecord",
    "SecurityScanRecord",
    "SkillConflict",
    "SkillRecord",
    "SkillScanUnavailable",
    "SkillScope",
    "SkillVersionRecord",
    "StagedFile",
    "StagedFileInfo",
    "VisibleSkill",
]
