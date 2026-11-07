"""技能扫描与 live registry（M7 §3.4）。

扫描规则本身（包边界、目录名、符号链接）在 scan.py：内置技能与 public/custom 共用
同一份，规则只写一遍。这里负责 **两个 category 的合并**（custom 覆盖 public）
以及 **内置技能 overlay**：内置技能只受 agent 白名单限制，同名时压过 catalog 行，
读 catalog 失败时照样留在索引里。

load_skills() **每次调用都扫盘**，不缓目录：技能刚被运维加进来，下一次组装提示词
就该出现在索引里。文件系统来源不再有全局/个人 JSON 开关。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.builtin import load_builtin_skills
from agentkit.skills.scan import is_within, scan_skill_category
from agentkit.skills.types import Skill, SkillCategory, SkillPathError

if TYPE_CHECKING:
    from agentkit.skills.catalog import SkillSnapshot

logger = logging.getLogger(__name__)

#: 已经告警过的同名冲突（内置技能盖住 catalog 行）。提示词每次模型调用都会重建
#: registry 视图；同一条 WARNING 每次刷一遍等于没有告警。reload 时清空。
_warned_builtin_collisions: set[str] = set()


def clear_builtin_collision_warnings() -> None:
    """清空 registry 的同名冲突告警去重；POST /api/skills/reload 时和内置缓存一起清。"""
    _warned_builtin_collisions.clear()


def _warn_builtin_collision(name: str, category: SkillCategory) -> None:
    if name in _warned_builtin_collisions:
        return
    _warned_builtin_collisions.add(name)
    logger.warning("Skill %s: builtin hides the %s row", name, category.value)


def load_skills(config: SkillsConfig) -> list[Skill]:
    """**每次调用都扫盘**（不缓目录）。返回值已做 custom > public 的覆盖。"""
    found: dict[str, Skill] = {}
    root = config.root_dir()
    for category in (SkillCategory.PUBLIC, SkillCategory.CUSTOM):  # custom 后写，覆盖 public
        for skill in scan_skill_category(
            root / category.value,
            category=category,
            container_path=config.container_path,
            enabled=True,
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
    # 文件系统来源只是本地调试/只读目录：没有全局或个人 JSON 开关。
    return list(found.values())


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
    snapshot: "SkillSnapshot | None" = None
    #: 注入点：给了就用它，没给就按 config 扫（loader 自带指纹缓存）。测试固定内置技能用。
    builtin_skills: tuple[Skill, ...] | None = None

    def _snapshot_skills(self, *, available_skills: frozenset[str] | None) -> list[Skill]:
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
                _warn_builtin_collision(skill.name, skill.category)
        return [*builtins, *(skill for skill in catalog if skill.name not in names)]

    def _catalog_skills(self) -> list[Skill]:
        """catalog/filesystem 的原始行：不含内置 overlay，也不做个人过滤。"""
        if self.snapshot is not None:
            return self._snapshot_skills(available_skills=None)
        if self.config is None:
            raise ValueError("SkillRegistry needs config when no snapshot is supplied")
        return load_skills(self.config)

    def _catalog_visible(self) -> list[Skill]:
        """现有可见性逻辑：两层开关 + agent 白名单，只作用于 catalog/filesystem 行。"""
        if self.snapshot is not None:
            return self._snapshot_skills(available_skills=self.available_skills)
        allowed = self.available_skills
        return [
            skill
            for skill in self._catalog_skills()
            if allowed is None or skill.name in allowed
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
        也不隐藏同名的 catalog 行——用户技能恰好和内置技能同名时检索仍要能找到它。
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