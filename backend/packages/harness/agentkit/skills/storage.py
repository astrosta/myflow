"""技能扫描与 live registry（M7 §3.4）。

扫描规则本身（包边界、目录名、符号链接）在 scan.py：内置技能与 public/custom 共用
同一份，规则只写一遍。这里负责 **两个 category 的合并**（custom 覆盖 public）、
把全局开关落到 Skill.enabled 上，以及 **内置技能 overlay**：内置技能没有全局/个人开关，
只受 agent 白名单限制，同名时压过 catalog 行，读 catalog 失败时照样留在索引里。

load_skills() **每次调用都扫盘**，不缓目录：技能刚被运维加进来，下一次组装提示词
就该出现在索引里。缓的是 JSON 里的两层开关。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.builtin import load_builtin_skills
from agentkit.skills.domain import SkillScope
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

    Built-in skills are a separate overlay, not a catalog generation: they are
    merged on every read (injectable via builtin_skills for tests), ordered
    before catalog rows, and kept even when the catalog itself fails.
    """

    config: SkillsConfig | None = None
    user_id: str | None = None
    available_skills: frozenset[str] | None = None
    prefs_path: Path | None = None
    _enabled_map: Mapping[str, bool] | None = field(default=None, repr=False)
    snapshot: "SkillSnapshot | None" = None
    #: 注入点：给了就用它，没给就按 config 扫（loader 自带指纹缓存）。测试固定内置技能用。
    builtin_skills: tuple[Skill, ...] | None = None

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

    def _builtin_skills(self) -> tuple[Skill, ...]:
        """内置技能：注入的 tuple 优先，否则按当前配置扫。"""
        if self.builtin_skills is not None:
            return self.builtin_skills
        if self.config is None:
            return ()
        return load_builtin_skills(self.config)

    def _visible_builtins(self) -> list[Skill]:
        """内置技能只受 agent 白名单限制：不读全局开关，也不读个人偏好。"""
        try:
            rows = self._builtin_skills()
        except Exception:  # loader 已经按失败退化；overlay 再兜一层，提示词不该 500
            logger.exception("Built-in skill overlay unavailable")
            return []
        allowed = self.available_skills
        return [skill for skill in rows if allowed is None or skill.name in allowed]

    def _merge_builtins(self, catalog: Sequence[Skill]) -> list[Skill]:
        """内置技能排在 catalog 之前；同名的 catalog 行被隐藏（内置优先）。"""
        builtins = self._visible_builtins()
        if not builtins:
            return list(catalog)
        names = {skill.name for skill in builtins}
        for skill in catalog:
            if skill.name in names:
                logger.warning(
                    "Skill %s: builtin hides the %s row", skill.name, skill.category.value
                )
        return [*builtins, *(skill for skill in catalog if skill.name not in names)]

    def _catalog_skills(self) -> list[Skill]:
        """catalog/filesystem 的原始行：不含内置 overlay，也不做个人过滤。"""
        if self.snapshot is not None:
            return self._snapshot_skills(prefs=None, available_skills=None)
        if self.config is None:
            raise ValueError("SkillRegistry needs config when no snapshot is supplied")
        return load_skills(self.config, enabled_map=self.enabled_map())

    def _catalog_visible(self) -> list[Skill]:
        """现有可见性逻辑：两层开关 + agent 白名单，只作用于 catalog/filesystem 行。"""
        if self.snapshot is not None:
            return self._snapshot_skills(
                prefs=self.prefs(), available_skills=self.available_skills
            )
        prefs = self.prefs()
        allowed = self.available_skills
        return [
            skill
            for skill in self._catalog_skills()
            if effective_enabled(skill, prefs) and (allowed is None or skill.name in allowed)
        ]

    def load(self) -> list[Skill]:
        """所有已知技能（内置在前）：不做个人过滤，catalog 读不出来时照旧抛。"""
        return self._merge_builtins(self._catalog_skills())

    def visible(self, *, include_builtin: bool = True) -> list[Skill]:
        """对当前用户**有效启用**、且在该 agent 白名单里的技能。

        返回内置技能在前、catalog 行在后。加载失败 → 失败的那部分是空的（fail-closed），
        但 fail-closed 只针对 catalog：内置技能是独立 overlay，Git runtime 或文件系统
        读不出来时它仍然留在索引和 slash 里。

        include_builtin=False 是**检索**口径：只返回 catalog 行本身，不加载内置技能，
        也不隐藏同名的 catalog 行——公共技能恰好和内置技能同名时检索仍要能找到它。
        """
        try:
            catalog = self._catalog_visible()
        except Exception:  # 配置读不出来 / 路径解析失败：宁可模型"不知道有这个技能"
            logger.exception("Skill registry unavailable; failing closed")
            return self._visible_builtins() if include_builtin else []
        return self._merge_builtins(catalog) if include_builtin else catalog

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
