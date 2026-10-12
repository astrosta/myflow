"""L1：describe_skill 的元数据（M7 §3.5）。

**只给元数据，不给正文**：正文进工具结果就会进 checkpoint 和后续每一次模型调用，
等于绕开"正文按需"。要正文就 read_file —— 那条路径同时完成自主激活（§3.10）。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentkit.skills.catalog import DEFAULT_CATALOG_LIMIT, search_skills
from agentkit.skills.types import Skill


def describe_payload(skill: Skill) -> dict:
    return {
        "name": skill.name,
        "description": skill.description,  # 完整，不截断
        "allowed_tools": None if skill.allowed_tools is None else list(skill.allowed_tools),
        "argument_hint": skill.argument_hint,
        "required_secrets": [
            {"name": item.name, "optional": item.optional} for item in skill.required_secrets
        ],
        "license": skill.license,
        "dir": skill.container_dir,
        "category": skill.category.value,
    }


def describe_skills(
    skills: Sequence[Skill], query: str | None, *, limit: int = DEFAULT_CATALOG_LIMIT
) -> list[dict]:
    return [describe_payload(skill) for skill in search_skills(skills, query, limit=limit)]
