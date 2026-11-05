"""L0 技能索引：系统提示词里唯一提到技能的地方（M7 §3.5）。

索引是有界的、唯一的形态：每技能一行，描述截断，正文一律按需（read_file 或 slash 注入）。
五条规则：

1. 只列**对当前用户有效启用**、且在 agent 白名单里的技能，从 live registry 现算；
   内置技能是唯一的例外——它没有个人开关，永远在索引里（方括号标 [builtin]）。
2. 排序稳定 (category, name)：加一个技能只该多出它自己那一行，否则前缀缓存在第一处变动起全失效。
3. 只截断描述（config.index_description_max_chars），其余字段根本不进索引。
4. **根目录只写一次**：块里不放每个技能的路径（几十条技能就是几十份重复前缀），
   根目录 + "目录名即技能名 + 固定 SKILL.md" 的约定放在块前面的说明里，路径可推。
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
from agentkit.skills.catalog import get_skill_snapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill
from agentkit.skills.usage import top_skill_ids

logger = logging.getLogger(__name__)

SKILL_INDEX_HEADER, SKILL_INDEX_FOOTER = "<available_skills>", "</available_skills>"

#: 索引前面的两句话。只说明"**技能放在哪**"和"**你在看什么**"，不说明"什么时候去读"——
#: 后者是工具 description 的职责（两套契约会漂）。三条取舍：
#: 1. **根目录写一次，不是每行写一遍**：`dir=` 曾经跟在每一行后面，几十个技能就是几十份重复前缀；
#:    给根目录 + 两条固定约定（目录名即技能名、SOP 固定叫 SKILL.md），路径照样可推，token 只花一次。
#: 2. **"索引不是正文"必须说**：两个标签都叫 skills，模型把索引那行当正文就会凭 120 字描述硬猜。
#: 3. **静态**：跟着索引一起进 LRU 缓存，不随每轮变化打穿前缀缓存。
SKILL_ROOT_CONVENTION = (
    "技能根目录 `{root}`：每个技能一个目录，**目录名就是技能名**，SOP 固定是该目录下的 "
    "`SKILL.md`，即 `{root}/<category>/<技能名>/SKILL.md`（category 是 public 或 custom，"
    "写在索引行的方括号里；同名时 custom 覆盖 public）。"
)
SKILL_CATALOG_ROOT_CONVENTION = (
    "技能根目录：你的私有技能在 `{private_root}`。"
    "每个技能一个目录，**目录名就是技能名**，SOP 固定是该目录下的 `SKILL.md`，"
    "即 `{private_root}/<name>/SKILL.md`；索引行方括号里的 private 标明作用域。"
)
#: filesystem 源 + 内置技能：public / custom / builtin 三个目录（同一棵树里）。
SKILL_FILESYSTEM_BUILTIN_ROOT_CONVENTION = (
    "技能根目录 {root}：公共技能在 {root}/public/<name>/SKILL.md，自定义技能在 "
    "{root}/custom/<name>/SKILL.md，内置技能在 {root}/builtin/<name>/SKILL.md；"
    "目录名就是技能名，同名时内置技能优先于 custom 和 public；custom 仍覆盖 public。"
)
#: catalog 快照（git 源）同时有内置技能：private / builtin 两个根。
SKILL_CATALOG_BUILTIN_ROOT_CONVENTION = (
    "技能根目录：你的私有技能在 `{private_root}`，内置技能在 `{builtin_root}`。"
    "每个技能一个目录，**目录名就是技能名**，SOP 固定是该目录下的 `SKILL.md`，"
    "即 `{private_root}/<name>/SKILL.md`（私有）或 `{builtin_root}/<name>/SKILL.md`（内置）；"
    "索引行方括号里的 private / builtin 标明作用域，同名时内置技能优先。"
)
SKILL_INDEX_PREAMBLE = (
    "下面是技能索引 `<available_skills>`：每个已启用技能一行，只有名字、类别、"
    "skill_id、路径和截断描述（需要密钥的会标出来），**不含正文**。私有技能超过 "
    "10 个时只列最常用的 10 个。已加载技能的名字与正文在持久上下文的 "
    "`<active_skills>` 里。"
)

_CACHE_MAX_ENTRIES = 32
_index_cache: "OrderedDict[str, str]" = OrderedDict()


def _entry_marker(skill: Skill) -> tuple[str, str]:
    """Return (name suffix, trailing hint) for one index row.

    A catalog skill is marked by visibility scope; legacy filesystem skills keep
    the legacy public/custom category marker.  A directory name that differs from the
    frontmatter name would make the root convention point at the wrong path, so
    that row gets an explicit dir=.
    """
    scope = getattr(skill.scope, "value", skill.scope)
    marker = scope if scope is not None else skill.category.value
    if scope == SkillScope.PRIVATE.value:
        return (
            f" [{marker}]",
            f" | id={skill.skill_id or '-'} | path={skill.container_file}",
        )
    if skill.relative_path.as_posix() == skill.name:
        return f" [{marker}]", ""
    return "", f" | dir={skill.container_dir}"


def render_skill_index(skills: Sequence[Skill], *, max_description_chars: int) -> str:
    """技能为 0 时返回空串。每行：- <name> [<category>]: <描述截断> [| 需密钥 A, B]

    路径**不在行里**：根目录与"目录名即技能名 + SKILL.md"的约定在块前面的说明里（见
    `SKILL_ROOT_CONVENTION`）。唯一例外是目录名和 name 对不上——那种技能直接给 `dir=`。
    """
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
        name_suffix, tail = _entry_marker(skill)
        lines.append(
            f"- {html.escape(skill.name)}{name_suffix}: {html.escape(description)}{secrets}{tail}"
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
    # 根目录说明随 source 变（git 用 catalog 文本，filesystem 用目录文本），
    # 同一批技能在两个源下的段落并不相同，缓存键必须带上它。
    key = f"{config.source}:{_cache_key(skills, config.index_description_max_chars)}"
    cached = _index_cache.get(key)
    if cached is not None:
        _index_cache.move_to_end(key)
        return cached
    index = render_skill_index(skills, max_description_chars=config.index_description_max_chars)
    section = (
        f"{_root_convention(skills, config)}\n{SKILL_INDEX_PREAMBLE}\n\n{index}"
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
    """Keep builtins, cap private skills at the Redis/DB top-K when over limit."""
    private = [
        skill
        for skill in skills
        if getattr(skill.scope, "value", skill.scope) == SkillScope.PRIVATE.value
    ]
    others = [skill for skill in skills if skill not in private]
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


def _root_convention(skills: Sequence[Skill], config: SkillsConfig) -> str:
    """按"来源 + 实际 scope"选根目录说明。

    catalog 行（private scope）用一段或两段文本；纯 filesystem 的 public/custom
    目录用旧的一段文本——**没有内置技能时措辞一个字不改**，旧提示词与旧测试都不动。
    内置技能是独立 overlay：它出现在 filesystem 索引里时只是多一个根，不改变其余约定。
    """
    scopes = {getattr(skill.scope, "value", skill.scope) for skill in skills}
    has_builtin = SkillScope.BUILTIN in scopes
    has_catalog_scope = SkillScope.PRIVATE in scopes
    root = config.container_path.rstrip("/") or config.container_path
    if config.source == "git" or has_catalog_scope:
        if has_builtin:
            return SKILL_CATALOG_BUILTIN_ROOT_CONVENTION.format(
                private_root=f"{root}/private",
                builtin_root=f"{root}/builtin",
            )
        return SKILL_CATALOG_ROOT_CONVENTION.format(
            private_root=f"{root}/private",
        )
    if has_builtin:
        return SKILL_FILESYSTEM_BUILTIN_ROOT_CONVENTION.format(root=root)
    return SKILL_ROOT_CONVENTION.format(root=config.container_path)


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
