"""技能检索：describe_skill 的 catalog（M7 §3.5）。

三种形态：
* select:a,b   —— 全部点名的，**无上限**（模型明确要哪些就给哪些）
* +prefix      —— 前缀匹配，最多 5 条
* 自由文本     —— 名字/描述的正则，最多 5 条；**非法 regex 退化成字面子串，不 500**
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agentkit.skills.types import Skill

DEFAULT_CATALOG_LIMIT = 5
SELECT_PREFIX = "select:"
NAME_PREFIX = "+"


def search_skills(
    skills: Sequence[Skill], query: str | None, *, limit: int = DEFAULT_CATALOG_LIMIT
) -> list[Skill]:
    pool = list(skills)
    text = (query or "").strip()
    if not text:
        return pool[:limit]
    if text.startswith(SELECT_PREFIX):
        names = [item.strip() for item in text[len(SELECT_PREFIX) :].split(",") if item.strip()]
        by_name = {skill.name: skill for skill in pool}
        return [by_name[name] for name in names if name in by_name]  # 无上限
    if text.startswith(NAME_PREFIX) and len(text) > len(NAME_PREFIX):
        prefix = text[len(NAME_PREFIX) :].strip().lower()
        return [skill for skill in pool if skill.name.lower().startswith(prefix)][:limit]
    matcher = text_matcher(text)
    return [skill for skill in pool if matcher(skill)][:limit]


def text_matcher(text: str):
    """非法 regex 退化成字面子串匹配：搜索词不该让工具 500。"""
    try:
        pattern = re.compile(text, re.IGNORECASE)
    except re.error:
        needle = text.lower()
        return lambda skill: needle in skill.name.lower() or needle in skill.description.lower()
    return lambda skill: bool(pattern.search(skill.name) or pattern.search(skill.description))
