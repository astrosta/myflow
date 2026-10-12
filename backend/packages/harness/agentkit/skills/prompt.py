"""L0 技能索引：系统提示词里唯一提到技能的地方（M7 §3.5）。

索引是有界的、唯一的形态：每技能一行，描述截断，正文一律按需（read_file 或 slash 注入）。
五条规则：

1. 只列**对当前用户有效启用**、且在 agent 白名单里的技能，从 live registry 现算。
2. 排序稳定 (category, name)：加一个技能只该多出它自己那一行，否则前缀缓存在第一处变动起全失效。
3. 只截断描述（config.index_description_max_chars），其余字段根本不进索引。
4. dir= 是**容器路径**，不是宿主路径。
5. 需要密钥的技能必须标出来，否则模型会去激活一个注定失败的技能。

0 个技能 → 整个块不出现（不留空标签给模型猜）。name/description 都 html.escape：
技能作者写的内容同样不可信，描述里可以塞 system-reminder 之类的伪造标签。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from collections import OrderedDict
from collections.abc import Sequence

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill

logger = logging.getLogger(__name__)

SKILL_INDEX_HEADER, SKILL_INDEX_FOOTER = "<available_skills>", "</available_skills>"

_CACHE_MAX_ENTRIES = 32
_index_cache: "OrderedDict[str, str]" = OrderedDict()


def render_skill_index(skills: Sequence[Skill], *, max_description_chars: int) -> str:
    """技能为 0 时返回空串。每行：- <name>: <描述截断> [| 需密钥 A, B] | dir=<容器路径>"""
    if not skills:
        return ""
    lines = [SKILL_INDEX_HEADER]
    for skill in sorted(skills, key=lambda item: (item.category.value, item.name)):
        description = _truncate(skill.description, max_description_chars)
        secrets = (
            " | 需密钥 " + ", ".join(item.name for item in skill.required_secrets)
            if skill.required_secrets
            else ""
        )
        lines.append(
            f"- {html.escape(skill.name)}: {html.escape(description)}{secrets} | dir={skill.container_dir}"
        )
    lines.append(SKILL_INDEX_FOOTER)
    return "\n".join(lines)


def build_skills_section(
    *,
    config: SkillsConfig,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
    registry: SkillRegistry | None = None,
) -> str:
    """系统提示词的技能块。注册表加载失败 → 空串（fail-closed，§3.5 规则 1）。"""
    live = registry or SkillRegistry(
        config=config, user_id=user_id, available_skills=available_skills
    )
    try:
        skills = live.visible()
    except Exception:
        logger.exception("Skill registry unavailable; rendering an empty index")
        return ""
    key = _cache_key(skills, config.index_description_max_chars)
    cached = _index_cache.get(key)
    if cached is not None:
        _index_cache.move_to_end(key)
        return cached
    section = render_skill_index(skills, max_description_chars=config.index_description_max_chars)
    _index_cache[key] = section
    while len(_index_cache) > _CACHE_MAX_ENTRIES:
        _index_cache.popitem(last=False)
    return section


def clear_skill_prompt_cache() -> None:
    """POST /api/skills/reload 的落点之一：只清本进程的 prompt 段落 LRU。"""
    _index_cache.clear()


def _cache_key(skills: Sequence[Skill], max_description_chars: int) -> str:
    material = json.dumps(
        [
            [
                skill.name,
                skill.description,
                skill.category.value,
                skill.container_dir,
                [item.name for item in skill.required_secrets],
            ]
            for skill in sorted(skills, key=lambda item: (item.category.value, item.name))
        ],
        ensure_ascii=False,
    )
    digest = hashlib.sha256(f"{max_description_chars}\n{material}".encode("utf-8")).hexdigest()
    return digest[:32]


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"
