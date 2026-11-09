"""L0 技能索引：系统提示词里唯一提到技能的地方（M7 §3.5）。

索引是有界的、唯一的形态：每技能一行，只放 name | description | id；不解释技能源，
也不写路径。正文一律按需，由模型调用 `load_skill(name, id)` 加载；内置技能
的 id 固定为 `builtin`，加载时按 name 定位。五条规则：

1. 只列**对当前用户有效启用**、且在 agent 白名单里的技能，从 live registry 现算；
   内置技能没有个人开关，永远在索引里。
2. 排序稳定 (category, name)：加一个技能只该多出它自己那一行，否则前缀缓存在第一处变动起全失效。
3. 只截断描述（config.index_description_max_chars），正文和路径都不进索引。
4. id 是加载参数：私有技能用内部 skill_id，内置技能统一用 `builtin`。
5. 需要密钥的技能必须标出来，否则模型会去激活一个注定失败的技能。

0 个技能 → 整个块不出现（不留空标签给模型猜）。name/description/id 都 html.escape：
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
from agentkit.skills.catalog import get_skill_snapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import BUILTIN_SKILL_ID, Skill
from agentkit.skills.usage import top_skill_ids

logger = logging.getLogger(__name__)

SKILL_INDEX_HEADER, SKILL_INDEX_FOOTER = "<available_skills>", "</available_skills>"

#: 索引前面的说明：只说明字段和加载入口，不说明技能源或路径。
#: 1. 三个字段固定为 name | description | id，模型不需要解析目录结构。
#: 2. "索引不是正文"必须说：否则模型会把 120 字描述当 SOP。
#: 3. 静态：跟着索引一起进 LRU 缓存，不随每轮变化打穿前缀缓存。
SKILL_INDEX_PREAMBLE = (
    "下面是技能索引 `<available_skills>`：每行是 `name | description | id`，"
    "只有元数据，**不含正文**。需要正文时调用 `load_skill(name, id)`；"
    "id 必须使用该行给出的值。{limit_rule}"
    "已加载技能的名字与正文在持久上下文的 `<active_skills>` 里。"
)


def _limit_rule(limit: int) -> str:
    """0 = 私有技能不进入索引；正数 = 超过上限时只列最常用的 N 个。"""
    if limit <= 0:
        return "私有技能不列在本索引里；需要时先按任务意图检索。"
    return f"私有技能超过 {limit} 个时只列最常用的 {limit} 个。"


_CACHE_MAX_ENTRIES = 32
_index_cache: "OrderedDict[str, str]" = OrderedDict()


def _display_id(skill: Skill) -> str:
    """模型看到的 id：内置技能统一为 builtin，私有技能用内部 skill_id。"""
    scope = getattr(skill.scope, "value", skill.scope)
    if scope == SkillScope.BUILTIN.value:
        return BUILTIN_SKILL_ID
    return skill.skill_id or "-"


def _cell(value: str) -> str:
    """Escape one pipe-delimited cell; author text must not forge a fourth field."""
    return html.escape(value, quote=False).replace("|", "&#124;")


def render_skill_index(skills: Sequence[Skill], *, max_description_chars: int) -> str:
    """技能为 0 时返回空串。每行固定三个字段：- <name> | <描述截断> | <id>

    不写技能源、目录、path 或 category：加载只需要 name 和 id，路径由 load_skill
    在服务端解析。需要密钥的标记并入 description，避免模型激活注定失败的技能，
    同时不破坏三字段格式。
    """
    if not skills:
        return ""
    lines = [SKILL_INDEX_HEADER]
    for skill in sorted(skills, key=lambda item: (item.category.value, item.name)):
        description = _truncate(skill.description, max_description_chars)
        if skill.required_secrets:
            description += "（需密钥 " + ", ".join(
                item.name for item in skill.required_secrets
            ) + "）"
        lines.append(
            f"- {_cell(skill.name)} | {_cell(description)} | {_cell(_display_id(skill))}"
        )
    lines.append(SKILL_INDEX_FOOTER)
    return "\n".join(lines)


def _default_registry(
    *,
    config: SkillsConfig,
    user_id: str | None,
    available_skills: frozenset[str] | None,
) -> SkillRegistry:
    """没显式给 registry 时按技能源取视图。

    git 源（生产）只能读进程里的快照：磁盘上没有可渲染的技能树，回退扫描只会渲染出
    遗留目录。空快照就是空索引，绝不扫盘。filesystem 源保留逐次扫盘的旧行为。
    """
    snapshot = get_skill_snapshot() if config.source == "git" else None
    return SkillRegistry(
        config=config,
        user_id=user_id,
        available_skills=available_skills,
        snapshot=snapshot,
    )


def build_skills_section(
    *,
    config: SkillsConfig,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
    registry: SkillRegistry | None = None,
) -> str:
    """系统提示词的技能块。注册表加载失败 → 空串（fail-closed，§3.5 规则 1）。"""
    live = registry or _default_registry(
        config=config, user_id=user_id, available_skills=available_skills
    )
    try:
        skills = _select_prompt_skills(
            live.visible(),
            user_id=user_id,
            limit=config.usage.prompt_top_k,
        )
    except Exception:
        logger.exception("Skill registry unavailable; rendering an empty index")
        return ""
    # 索引不再随技能源变化：同一批技能只渲染一种 name | description | id 形态。
    # prompt_top_k 会出现在说明文字里，缓存键必须带上它。
    key = f"{config.usage.prompt_top_k}:{_cache_key(skills, config.index_description_max_chars)}"
    cached = _index_cache.get(key)
    if cached is not None:
        _index_cache.move_to_end(key)
        return cached
    index = render_skill_index(skills, max_description_chars=config.index_description_max_chars)
    section = (
        f"{SKILL_INDEX_PREAMBLE.format(limit_rule=_limit_rule(config.usage.prompt_top_k))}\n\n{index}"
        if index
        else ""
    )
    _index_cache[key] = section
    while len(_index_cache) > _CACHE_MAX_ENTRIES:
        _index_cache.popitem(last=False)
    return section


def clear_skill_prompt_cache() -> None:
    """POST /api/skills/reload 的落点之一：只清本进程的 prompt 段落 LRU。"""
    _index_cache.clear()


def _select_prompt_skills(
    skills: Sequence[Skill],
    *,
    user_id: str | None,
    limit: int,
) -> list[Skill]:
    """Keep non-private rows; limit=0 drops private rows without reading usage."""
    private = [
        skill
        for skill in skills
        if getattr(skill.scope, "value", skill.scope) == SkillScope.PRIVATE.value
    ]
    others = [skill for skill in skills if skill not in private]
    if limit <= 0:
        # 0 = 不把私有技能放进提示词；没有东西可排序，也就不该读 Redis。
        return list(others)
    if len(private) <= limit:
        return [*others, *private]

    by_id = {skill.skill_id: skill for skill in private if skill.skill_id}
    selected: list[Skill] = []
    for skill_id in top_skill_ids(user_id) or ():
        skill = by_id.get(skill_id)
        if skill is not None and skill not in selected:
            selected.append(skill)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        chosen = {skill.skill_id for skill in selected}
        fallback = sorted(
            (skill for skill in private if skill.skill_id not in chosen),
            key=lambda skill: (
                -int(skill.load_count or 0),
                skill.name,
                skill.skill_id or "",
            ),
        )
        selected.extend(fallback[: limit - len(selected)])
    return [*others, *selected]


def _cache_key(skills: Sequence[Skill], max_description_chars: int) -> str:
    material = json.dumps(
        [
            [
                skill.name,
                skill.description,
                skill.category.value,
                getattr(skill.scope, "value", skill.scope),
                skill.owner_user_id,
                skill.version_id,
                skill.skill_id,
                skill.load_count,
                skill.container_dir,
                skill.container_file,
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
