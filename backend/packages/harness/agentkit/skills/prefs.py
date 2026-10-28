"""有效启用状态与个人偏好落盘（M7 §2.4）。

    effective_enabled(user, skill) = global_enabled(skill) and user_pref(user, skill)

两层不能互相覆盖：用户不能把一个被全局禁用的技能打开（全局禁用往往意味着这个部署
缺依赖或缺合规审批），管理员也不该替用户决定"你用不用"。
"""

from __future__ import annotations

from collections.abc import Mapping

from agentkit.config.extensions_config import get_extensions_config
from agentkit.config.skills_prefs import (
    DEFAULT_USER_BUCKET,
    read_skills_prefs,
    read_user_skill_prefs,
    resolve_skills_prefs_path,
    skills_prefs_file_lock,
    skills_prefs_write_lock,
    update_skills_prefs,
    user_bucket,
    writable_skills_prefs_path,
    write_user_skill_pref,
)
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import Skill

__all__ = [
    "DEFAULT_USER_BUCKET",
    "effective_enabled",
    "global_enabled_map",
    "preference_key",
    "read_skills_prefs",
    "read_user_skill_prefs",
    "resolve_skills_prefs_path",
    "skills_prefs_file_lock",
    "skills_prefs_write_lock",
    "update_skills_prefs",
    "user_bucket",
    "writable_skills_prefs_path",
]


def global_enabled_map() -> dict[str, bool]:
    """extensions_config.json 里 skills.<name>.enabled；缺省 true（扫描到的新技能默认开）。"""
    config = get_extensions_config()
    return {name: entry.enabled for name, entry in config.skills.items()}


def preference_key(skill: Skill) -> str:
    """Catalog skills are keyed by skill_id; legacy filesystem skills by name."""
    return skill.skill_id or skill.name


def effective_enabled(skill: Skill, prefs: Mapping[str, bool] | None = None) -> bool:
    """全局启用 **且** 个人没关掉。pref 缺省 true（没表过态就是开）。

    Catalog preferences are keyed by skill_id (Task 15 migration); a name-keyed
    entry is still accepted so filesystem skills and un-migrated preferences
    keep working.

    内置技能在两层之上：随项目发布、只读，部署级开关只有 skills.builtin.enabled，
    个人偏好与 extensions_config 的同名条目都不该让它消失。
    """
    if skill.scope == SkillScope.BUILTIN:
        return True
    if not skill.enabled:
        return False
    if prefs is None:
        return True
    if skill.skill_id and skill.skill_id in prefs:
        return bool(prefs[skill.skill_id])
    return bool(prefs.get(skill.name, True))
