"""技能目录 → SkillResponse（M7 §2.4/§3.5）。

列表（GET /api/skills）与模型可见索引（<available_skills>）**必须来自同一份扫描**
（agentkit.skills.storage.load_skills）：列表里有、索引里没有（或反过来）就是 bug。

enabled 返回的是**有效状态** = 全局 and 个人偏好（§2.4 的两层开关）；只有 admin 传
include_global=True 时才额外带上 global_enabled。
"""

from __future__ import annotations

import logging

from agentkit.config import get_app_config
from agentkit.config.extensions_config import (
    reset_extensions_config_cache,
    update_extensions_config,
)
from agentkit.config.skills_config import SkillsConfig
from agentkit.config.skills_prefs import read_user_skill_prefs, write_user_skill_pref
from agentkit.skills.prefs import effective_enabled, global_enabled_map
from agentkit.skills.prompt import clear_skill_prompt_cache
from agentkit.skills.storage import load_skills
from agentkit.skills.types import Skill
from backend.app.gateway.errors import SkillNotFound
from backend.app.gateway.models.response import SkillResponse

logger = logging.getLogger(__name__)


def skills_config() -> SkillsConfig:
    return get_app_config().skills


def scan_skill_records() -> list[Skill]:
    """磁盘上扫到的全部技能（含全局禁用的），带全局开关。"""
    return load_skills(skills_config(), enabled_map=global_enabled_map())


def _to_response(skill: Skill, *, user_id: str | None, include_global: bool) -> SkillResponse:
    prefs = read_user_skill_prefs(user_id)
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        category=skill.category.value,
        enabled=effective_enabled(skill, prefs),
        global_enabled=skill.enabled if include_global else None,
    )


def scan_skills_for_api(*, user_id: str | None, include_global: bool = False) -> list[SkillResponse]:
    return [
        _to_response(skill, user_id=user_id, include_global=include_global)
        for skill in sorted(scan_skill_records(), key=lambda item: item.name)
    ]


def _response_for(name: str, *, user_id: str | None, include_global: bool = False) -> SkillResponse:
    for skill in scan_skill_records():
        if skill.name == name:
            return _to_response(skill, user_id=user_id, include_global=include_global)
    raise SkillNotFound(f"Skill {name} not found")


def set_user_skill_pref(user_id: str | None, name: str, enabled: bool) -> SkillResponse:
    """个人开关（任何登录用户）：只写 skills_prefs.json。**不 reload 全局配置**——它没变。"""
    if name not in {skill.name for skill in scan_skill_records()}:
        raise SkillNotFound(f"Skill {name} not found")
    write_user_skill_pref(user_id, name, enabled)
    return _response_for(name, user_id=user_id)


def patch_skill_enabled(name: str, enabled: bool) -> SkillResponse:
    """全局开关（admin）：改 extensions_config.json 的 skills.<name>.enabled，动所有人。"""

    def mutate(data: dict) -> None:
        # 扫描在锁内：名字是不是真的存在，以写这一刻的磁盘为准（§2.4 步骤 3）
        scanned = {skill.name for skill in scan_skill_records()}
        if name not in scanned:
            raise SkillNotFound(f"Skill {name} not found")
        skills = data.get("skills")
        if not isinstance(skills, dict):
            skills = {}
        entry = skills.get(name)
        updated = dict(entry) if isinstance(entry, dict) else {}
        updated["enabled"] = bool(enabled)
        skills[name] = updated
        data["skills"] = skills

    update_extensions_config(mutate)
    clear_skill_prompt_cache()  # 索引立刻反映新状态，不重扫目录
    return _response_for(name, user_id=None, include_global=True)


def reload_skill_caches() -> None:
    """POST /api/skills/reload：只清本进程的 prompt 段落 LRU 与配置缓存。"""
    clear_skill_prompt_cache()
    reset_extensions_config_cache()
