"""共享的技能目录扫描器（M7 §3.4 / §3.1）。

三个 category（public / custom / builtin）共用同一条扫描规则，所以规则只写一遍：

* **技能目录是一个包边界**：遇到一个 SKILL.md 就停止（只扫 category 根的一级子目录），
  绝不 rglob —— 否则技能包里的示例、fixture、子模板都会变成独立技能。
* **符号链接分两种**：一级符号链接目录默认允许（运维的技能树常这么组织，target 可以在
  skills root 之外），但符号链接的 SKILL.md 和任何更深层的 resolve() 逃逸一律拒。
  `allow_symlink_dirs=False` 时连一级符号链接目录也拒（内置技能随代码发布，
  没有运维需要把它放到别处）。
* **一个坏技能不拖垮全部**：非法目录名、坏 frontmatter 只跳过那一个。

调用方只负责"扫哪几个 category、开关怎么来"：load_skills() 管 public/custom 与
custom > public 的覆盖；builtin 加载器（§3.1）管它自己的缓存。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from agentkit.skills.parser import SkillParseError, parse_skill_file
from agentkit.skills.types import SKILL_FILE_NAME, Skill, SkillCategory

if TYPE_CHECKING:
    from agentkit.skills.domain import SkillScope

logger = logging.getLogger(__name__)

#: §2.4：技能目录名和 PATCH 的 name 用同一条正则，否则列表里有名字、PATCH 却 422。
SKILL_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

__all__ = ["SKILL_DIR_NAME_RE", "is_usable_entry", "is_within", "scan_skill_category"]


def scan_skill_category(
    category_root: Path,
    *,
    category: SkillCategory,
    container_path: str,
    scope: "SkillScope | None" = None,
    enabled: bool = False,
    allow_symlink_dirs: bool = True,
) -> list[Skill]:
    """扫一个 category 根目录的**一级子目录**，按目录名稳定排序返回技能。

    根目录不存在 → `[]`（运维还没建目录不是错误）。同名目录只保留排序在后的那个。
    """
    if not category_root.is_dir():
        return []
    found: dict[str, Skill] = {}
    for entry in sorted(category_root.iterdir(), key=lambda item: item.name):  # 稳定顺序
        skill_file = entry / SKILL_FILE_NAME
        if not SKILL_DIR_NAME_RE.match(entry.name):
            logger.warning("Skipping skill directory with an illegal name: %s", entry)
            continue
        # 包边界 + 符号链接校验：没有 SKILL.md 的子目录（examples/、fixtures/）在这里被挡住
        if not is_usable_entry(
            entry, skill_file, category_root, allow_symlink_dirs=allow_symlink_dirs
        ):
            continue
        try:
            parsed = parse_skill_file(skill_file)
        except (SkillParseError, OSError, yaml.YAMLError, UnicodeError) as exc:
            # 一个坏技能不拖垮全部：畸形 YAML 与非法编码和坏 frontmatter 一样只跳过这一个。
            logger.warning("Skipping skill %s: %s", entry, exc)
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
                "Skill %s: %s overrides the earlier directory %s",
                parsed.name,
                entry.name,
                previous.skill_dir.name,
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
            enabled=enabled,
            container_path=container_path,
            scope=scope,
        )
    return list(found.values())


def is_usable_entry(
    entry: Path,
    skill_file: Path,
    category_root: Path,
    *,
    allow_symlink_dirs: bool = True,
) -> bool:
    """一级符号链接目录默认允许；符号链接的 SKILL.md 和更深层的逃逸一律拒绝。"""
    if not skill_file.is_file():
        return False
    if skill_file.is_symlink():
        logger.warning("Refusing symlinked %s", skill_file)
        return False
    if not allow_symlink_dirs and entry.is_symlink():
        logger.warning("Refusing symlinked skill directory %s", entry)
        return False
    entry_resolved = entry.resolve()
    if not is_within(entry_resolved, category_root.resolve()):
        # 只有 category 根下的一级符号链接可以指到外面
        if entry.is_symlink() and entry.parent == category_root:
            logger.debug("Skill directory %s is a first-level symlink to %s", entry, entry_resolved)
        else:
            logger.warning("Refusing skill outside the skills root: %s", entry)
            return False
    if not is_within(skill_file.resolve(), entry_resolved):
        logger.warning("Refusing %s: it escapes its skill directory", skill_file)
        return False
    return True


def is_within(path: Path, root: Path) -> bool:
    """`path` 是否落在 `root` 里（两者都应已 resolve）。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
