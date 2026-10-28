"""技能的数据类型（M7 §3.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from agentkit.config.skills_config import DEFAULT_SKILLS_CONTAINER_PATH

if TYPE_CHECKING:
    from agentkit.skills.domain import SkillScope

SKILL_FILE_NAME = "SKILL.md"


class SkillCategory(StrEnum):
    PUBLIC = "public"
    CUSTOM = "custom"
    BUILTIN = "builtin"
    # INTEGRATION / LEGACY —— M7 不实现，枚举值先占位以免字符串漂移


@dataclass(frozen=True, slots=True)
class SecretRequirement:
    """name 既是 context.secrets 的键，也是注入脚本时的环境变量名（§4）。"""

    name: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class Skill:
    """部署级的技能对象。**没有"用户的开关"这种字段**——那是 skills_prefs.json（§2.4）。"""

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path
    category: SkillCategory
    allowed_tools: tuple[str, ...] | None = None  # None = 未声明；() = 声明了空列表
    enabled: bool = False  # 全局开关，来自 extensions_config.json
    required_secrets: tuple[SecretRequirement, ...] = ()
    secrets_autonomous: bool = True
    argument_hint: str | None = None
    container_path: str = DEFAULT_SKILLS_CONTAINER_PATH
    skill_id: str | None = None
    scope: SkillScope | None = None
    owner_user_id: str | None = None
    version_id: str | None = None
    skill_version: str | None = None
    release_id: str | None = None
    git_repository: str | None = None
    git_commit: str | None = None
    repo_path: str | None = None

    @property
    def container_dir(self) -> str:
        """沙箱目录：catalog 技能用 public/private，filesystem 技能保留 category。"""
        scope = getattr(self.scope, "value", self.scope)
        root = scope if scope is not None else self.category.value
        return f"{self.container_path}/{root}/{self.relative_path.as_posix()}"

    @property
    def container_file(self) -> str:
        return f"{self.container_dir}/{SKILL_FILE_NAME}"

    @property
    def declared_allowed_tools(self) -> bool:
        """声明过 allowed-tools 吗？None 才算没声明（() 是"声明了空列表"）。"""
        return self.allowed_tools is not None


class SkillPathError(ValueError):
    """技能的路径不合法：符号链接的 SKILL.md、或 resolve() 之后的逃逸（§3.4）。"""
