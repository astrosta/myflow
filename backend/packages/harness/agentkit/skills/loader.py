"""技能正文的读取与预算工具（M7 §3.5）。

排序和检索都在 Milvus 与 `agentkit.skills.search` 里做；这里只负责两件事：

1. **读取**。正文按路径校验、UTF-8 解码、坏文件跳过，提示词投影与 `load_skill`
   共用同一份。
2. **预算**。单条有 `max_body_chars`，整体有 `max_context_chars`；超出就截断
   或丢弃，两种情况都要在返回里写清楚——模型以为拿到了全文、实际只拿到一半，
   比没拿到更糟。
"""

from __future__ import annotations

import logging

from agentkit.skills.parser import parse_skill_file
from agentkit.skills.storage import validate_skill_file_path
from agentkit.skills.types import Skill, SkillPathError

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
