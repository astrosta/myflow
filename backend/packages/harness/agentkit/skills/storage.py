"""技能扫描与 live registry（M7 §3.4）。

两条容易忽略的规则：

* **技能目录是一个包边界**：遇到一个 SKILL.md 就停止（只扫 category 根的一级子目录），
  绝不 rglob —— 否则技能包里的示例、fixture、子模板都会变成独立技能。
* **符号链接分两种**：一级符号链接目录允许（运维的技能树常这么组织，target 可以在
  skills root 之外），但符号链接的 SKILL.md 和任何更深层的 resolve() 逃逸一律拒。

load_skills() **每次调用都扫盘**，不缓目录：技能刚被运维加进来，下一次组装提示词
就该出现在索引里。缓的是 JSON 里的两层开关。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.parser import SkillParseError, parse_skill_file
from agentkit.skills.prefs import effective_enabled, global_enabled_map, read_user_skill_prefs
from agentkit.skills.types import SKILL_FILE_NAME, Skill, SkillCategory, SkillPathError

if TYPE_CHECKING:
    from agentkit.skills.catalog import SkillSnapshot

logger = logging.getLogger(__name__)

#: §2.4：技能目录名和 PATCH 的 name 用同一条正则，否则列表里有名字、PATCH 却 422。
SKILL_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def load_skills(config: SkillsConfig, *, enabled_map: Mapping[str, bool]) -> list[Skill]:
    """**每次调用都扫盘**（不缓目录）。返回值已做 custom > public 的覆盖。"""
    found: dict[str, Skill] = {}
    root = config.root_dir()
    for category in (SkillCategory.PUBLIC, SkillCategory.CUSTOM):  # custom 后写，覆盖 public
        category_root = root / category.value
        if not category_root.is_dir():
            continue
        for entry in sorted(category_root.iterdir(), key=lambda item: item.name):  # 稳定顺序
            skill_file = entry / SKILL_FILE_NAME
            if not SKILL_DIR_NAME_RE.match(entry.name):
                logger.warning("Skipping skill directory with an illegal name: %s", entry)
                continue
            if not _is_usable_entry(entry, skill_file, category_root):  # 包边界 + 符号链接校验
                continue
            try:
                parsed = parse_skill_file(skill_file)
            except (SkillParseError, OSError) as exc:
                logger.warning("Skipping skill %s: %s", entry, exc)  # 一个坏技能不拖垮全部
                continue
            if parsed.name != entry.name:
                # 目录名与 name 不一致时以 frontmatter 为准（§3.1）；name 的正则由 parser 管。
                logger.warning(
                    "Skill %s: directory %r != name %r; using frontmatter name",
                    skill_file,
                    entry.name,
                    parsed.name,
                )
            previous = found.get(parsed.name)
            if previous is not None:
                logger.warning(
                    "Skill %s: category %s overrides %s",
                    parsed.name,
                    category.value,
                    previous.category.value,
                )
            found[parsed.name] = Skill(
                name=parsed.name,
                description=parsed.description,
                license=parsed.license,
                argument_hint=parsed.argument_hint,
                skill_dir=entry,
                skill_file=skill_file,
                relative_path=entry.relative_to(category_root),
                category=category,
                allowed_tools=parsed.allowed_tools,
                required_secrets=parsed.required_secrets,
                secrets_autonomous=parsed.secrets_autonomous,
                enabled=enabled_map.get(parsed.name, True),  # 扫描到的新技能默认开
                container_path=config.container_path,
            )
    return list(found.values())


def _is_usable_entry(entry: Path, skill_file: Path, category_root: Path) -> bool:
    """一级符号链接目录允许；符号链接的 SKILL.md 和更深层的逃逸一律拒绝。"""
    if not skill_file.is_file():
        return False
    if skill_file.is_symlink():
        logger.warning("Refusing symlinked %s", skill_file)
        return False
    entry_resolved = entry.resolve()
    if not _is_within(entry_resolved, category_root.resolve()):
        # 只有 category 根下的一级符号链接可以指到外面
        if entry.is_symlink() and entry.parent == category_root:
            logger.debug("Skill directory %s is a first-level symlink to %s", entry, entry_resolved)
        else:
            logger.warning("Refusing skill outside the skills root: %s", entry)
            return False
    if not _is_within(skill_file.resolve(), entry_resolved):
        logger.warning("Refusing %s: it escapes its skill directory", skill_file)
        return False
    return True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_skill_file_path(skill: Skill) -> Path:
    """激活前再验一次（§3.6）：符号链接的 SKILL.md、逃逸、文件消失都拒。"""
    skill_file = skill.skill_file
    if skill_file.is_symlink():
        raise SkillPathError(f"refusing symlinked skill file: {skill_file}")
    try:
        resolved = skill_file.resolve(strict=True)
    except OSError as exc:
        raise SkillPathError(f"skill file is not readable: {skill_file}") from exc
    if not _is_within(resolved, skill.skill_dir.resolve()):
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
