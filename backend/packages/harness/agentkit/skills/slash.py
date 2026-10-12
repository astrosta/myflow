"""斜杠命令的技能解析（M7 §3.5/§3.6）。

只认**行首**、名字之后必须是空白或结束。解析器只负责语法：技能是否存在、是否启用、
是否在 agent 白名单里，是中间件的事。

这份正则和保留名同时进 contracts/slash_skill_contract.json，前端展示解析用同一份；
test_slash_skill_contract.py 负责双向钉死，一边改一边漏就红。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentkit.config.runtime_paths import project_root

CONTRACT_VERSION = 1
CONTRACT_RELATIVE_PATH = Path("contracts") / "slash_skill_contract.json"

#: 频道命令优先，不能当技能名。
RESERVED_SLASH_SKILL_NAMES = frozenset(
    {"agent", "bootstrap", "goal", "help", "memory", "models", "new", "status"}
)

#: 技能名正则：与 §3.1 的 frontmatter name 同一条。
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
SLASH_SKILL_COMMAND_PATTERN = r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)"

_SLASH_SKILL_RE = re.compile(SLASH_SKILL_COMMAND_PATTERN)


@dataclass(frozen=True, slots=True)
class SlashSkillMatch:
    name: str
    argument: str  # 斜杠命令之后的剩余文本（可能为空）


def parse_slash_skill(text: str | None) -> SlashSkillMatch | None:
    """返回 None 表示"这不是斜杠激活"。文本必须已经是用户原文（M6 §4.2）。"""
    if not text:
        return None
    match = _SLASH_SKILL_RE.match(text)  # ^/ 已经拒绝了前导空白
    if match is None:
        return None
    name = match.group(1)
    if name in RESERVED_SLASH_SKILL_NAMES:  # 频道命令优先，不能当技能名
        return None
    return SlashSkillMatch(name=name, argument=text[match.end() :].strip())


def slash_skill_contract() -> dict:
    """Python 侧的真值。测试拿它和 contracts/slash_skill_contract.json 对撞。"""
    return {
        "version": CONTRACT_VERSION,
        "name_pattern": SKILL_NAME_PATTERN,
        "command_pattern": SLASH_SKILL_COMMAND_PATTERN,
        "reserved_names": sorted(RESERVED_SLASH_SKILL_NAMES),
        "notes": [
            "只认行首：前导空白不是斜杠激活",
            "名字之后必须是空白或输入结束；/pdf-processingX 不是斜杠激活",
            "保留名是频道命令，永远不当技能",
        ],
    }


def contract_path() -> Path:
    return project_root() / CONTRACT_RELATIVE_PATH


def load_slash_skill_contract(path: Path | None = None) -> dict:
    target = path if path is not None else contract_path()
    return json.loads(target.read_text(encoding="utf-8"))
