"""技能扫描与 live registry（M7 §3.4）。

扫描规则本身（包边界、目录名、符号链接）在 scan.py：内置技能与 public/custom 共用
同一份，规则只写一遍。这里只负责 **两个 category 的合并**（custom 覆盖 public）和
把全局开关落到 Skill.enabled 上。

load_skills() **每次调用都扫盘**，不缓目录：技能刚被运维加进来，下一次组装提示词
就该出现在索引里。缓的是 JSON 里的两层开关。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.prefs import effective_enabled, global_enabled_map, read_user_skill_prefs
from agentkit.skills.scan import is_within, scan_skill_category
from agentkit.skills.types import Skill, SkillCategory, SkillPathError

if TYPE_CHECKING:
    from agentkit.skills.catalog import SkillSnapshot

logger = logging.getLogger(__name__)


def load_skills(config: SkillsConfig, *, enabled_map: Mapping[str, bool]) -> list[Skill]:
    """**每次调用都扫盘**（不缓目录）。返回值已做 custom > public 的覆盖。"""
    found: dict[str, Skill] = {}
    root = config.root_dir()
    for category in (SkillCategory.PUBLIC, SkillCategory.CUSTOM):  # custom 后写，覆盖 public
        for skill in scan_skill_category(
            root / category.value,
            category=category,
            container_path=config.container_path,
        ):
            previous = found.get(skill.name)
            if previous is not None:
                logger.warning(
                    "Skill %s: category %s overrides %s",
                    skill.name,
                    category.value,
                    previous.category.value,
                )
            found[skill.name] = skill
    # 全局开关在这里落到 Skill.enabled；个人偏好是 prefs 的事（§2.4），不进扫描。
    return [
        replace(skill, enabled=enabled_map.get(skill.name, True))  # 扫描到的新技能默认开
        for skill in found.values()
    ]


def validate_skill_file_path(skill: Skill) -> Path:
    """激活前再验一次（§3.6）：符号链接的 SKILL.md、逃逸、文件消失都拒。"""
    skill_file = skill.skill_file
    if skill_file.is_symlink():
        raise SkillPathError(f"refusing symlinked skill file: {skill_file}")
    try:
        resolved = skill_file.resolve(strict=True)
    except OSError as exc:
        raise SkillPathError(f"skill file is not readable: {skill_file}") from exc
    if not is_within(resolved, skill.skill_dir.resolve()):
        raise SkillPathError(f"skill file escapes its skill directory: {skill_file}")
    return resolved


@dataclass(frozen=True, slots=True)
class SkillRegistry:
    """Skill view for the prompt, slash, policy, and durable-context paths.

    With a snapshot, reads project that immutable catalog generation for this
    user; without one, the legacy filesystem scanner runs on every read so tests
    and filesystem-source deployments keep their old behavior.
    """

    config: SkillsConfig | None = None
    user_id: str | None = None
    available_skills: frozenset[str] | None = None
    prefs_path: Path | None = None
    _enabled_map: Mapping[str, bool] | None = field(default=None, repr=False)
    snapshot: "SkillSnapshot | None" = None

    def enabled_map(self) -> Mapping[str, bool]:
        return self._enabled_map if self._enabled_map is not None else global_enabled_map()

    def prefs(self) -> Mapping[str, bool]:
        return read_user_skill_prefs(self.user_id, path=self.prefs_path)

    def _snapshot_skills(
        self,
        *,
        prefs: Mapping[str, bool] | None,
        available_skills: frozenset[str] | None,
    ) -> list[Skill]:
        snapshot = self.snapshot
        if snapshot is None:  # pragma: no cover - guarded by callers
            return []
        return [
            replace(
                skill,
                container_path=(
                    self.config.container_path
                    if self.config is not None
                    else skill.container_path
                ),
            )
            for skill in snapshot._visible_for(
                self.user_id,
                available_skills=available_skills,
                prefs=prefs,
            )
        ]

    def load(self) -> list[Skill]:
        if self.snapshot is not None:
            return self._snapshot_skills(prefs=None, available_skills=None)
        if self.config is None:
            raise ValueError("SkillRegistry needs config when no snapshot is supplied")
        return load_skills(self.config, enabled_map=self.enabled_map())

    def visible(self) -> list[Skill]:
        """对当前用户**有效启用**、且在该 agent 白名单里的技能。加载失败 → 空（fail-closed）。"""
        if self.snapshot is not None:
            try:
                return self._snapshot_skills(
                    prefs=self.prefs(), available_skills=self.available_skills
                )
            except Exception:
                logger.exception("Skill snapshot unavailable; failing closed")
                return []
        try:
            skills = self.load()
        except Exception:  # 配置读不出来 / 路径解析失败：宁可模型"不知道有这个技能"
            logger.exception("Skill registry unavailable; failing closed")
            return []
        prefs = self.prefs()
        allowed = self.available_skills
        return [
            skill
            for skill in skills
            if effective_enabled(skill, prefs) and (allowed is None or skill.name in allowed)
        ]

    def find(self, name: str) -> Skill | None:
        """slash 解析：同名时私有 active 优先于公共行（custom 覆盖 public 的旧规则保留）。"""
        matches = [skill for skill in self.visible() if skill.name == name]
        for skill in matches:
            scope = getattr(skill.scope, "value", skill.scope)
            if scope == "private" and skill.owner_user_id == self.user_id:
                return skill
        return matches[0] if matches else None

    def index_by_container_file(self) -> dict[str, Skill]:
        return {skill.container_file: skill for skill in self.visible()}
