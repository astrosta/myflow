"""把检索结果装成模型读到的那一段：正文预算 + payload（M7 §3.5）。

排序在 `router` 里做，这里只做两件事：

1. **预算**。一个技能的正文可能上万字，`top_k` 个技能全塞进去就是一次小型的上下文事故。
   单条有 `max_body_chars`，整体有 `max_context_chars`；超出就截断（单条）或丢弃（整体装不下），
   两种情况都要在返回里写清楚——模型以为拿到了全文、实际只拿到一半，比没拿到更糟。
2. **形状**。模型要认出"这是哪个技能、正文从哪来、还能不能再往下读"：`path` 是容器里的
   SKILL.md 路径（可以再 `read_file` 读被截断的后半段），`dir` 是技能目录。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agentkit.config.skills_config import SkillSearchConfig
from agentkit.skills.parser import parse_skill_file
from agentkit.skills.storage import validate_skill_file_path
from agentkit.skills.types import Skill, SkillPathError

if TYPE_CHECKING:  # router 反向引用本模块的 read_skill_body，运行时不能互相 import
    from agentkit.skills.router import SearchOutcome

logger = logging.getLogger(__name__)

BODY_TRUNCATION_MARKER = "\n\n…（正文超出预算，已截断；完整内容请 read_file 读 path）"

#: 整体预算剩下不到这么多字符就不装了：半个标题没有任何操作价值。
MIN_USEFUL_BODY_CHARS = 200


def read_skill_body(skill: Skill) -> str | None:
    """读技能正文（去掉 frontmatter）。读不出来返回 None——**一个坏技能不该让整次投影失败**。

    检索（`router`）和 `<active_skills>` 投影（`durable_context_middleware`）共用这一份：
    路径校验、编码、坏文件跳过这三件事只写一遍。
    """
    try:
        path = validate_skill_file_path(skill)
        return parse_skill_file(path).body.strip()
    except (SkillPathError, ValueError, OSError) as exc:
        # ValueError 覆盖 SkillParseError 与非 UTF-8 的 UnicodeDecodeError
        logger.warning("Skipping skill %s: %s", skill.name, exc)
        return None


def clip_body(body: str, limit: int) -> tuple[str, bool]:
    """按字符预算截断。返回 (正文, 是否截断)。"""
    if limit <= 0 or len(body) <= limit:
        return body, False
    keep = max(limit - len(BODY_TRUNCATION_MARKER), 0)
    return body[:keep] + BODY_TRUNCATION_MARKER, True


def build_payload(outcome: "SearchOutcome", *, config: SkillSearchConfig) -> dict:
    """技能正文按预算装进 payload。整体预算装不下的技能进 `skipped`，不静默吞掉。"""
    skills: list[dict] = []
    skipped: list[str] = []
    remaining = config.max_context_chars
    for hit in outcome.hits:
        budget = min(config.max_body_chars, remaining)
        if budget < MIN_USEFUL_BODY_CHARS and budget < len(hit.body):
            skipped.append(hit.skill.name)  # 剩下这点预算连一段操作步骤都装不下
            continue
        body, truncated = clip_body(hit.body, budget)
        remaining -= len(body)
        skills.append(
            {
                "name": hit.skill.name,
                "description": hit.skill.description,
                "category": hit.skill.category.value,
                "dir": hit.skill.container_dir,
                "path": hit.skill.container_file,
                "score": hit.score,
                "channels": list(hit.channels),
                "body_truncated": truncated,
                "body": body,
            }
        )
    return {
        "skills": skills,
        "retrieval": {
            "channels": list(outcome.channels),
            "candidates": outcome.candidates,
            "reranked": outcome.reranked,
            "warnings": list(outcome.warnings),
            "skipped": skipped,
        },
    }
