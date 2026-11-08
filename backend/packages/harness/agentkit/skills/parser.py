"""SKILL.md 的 frontmatter 解析（M7 §3.3）。

技能文本**一律 UTF-8**：读写都显式 encoding="utf-8"，不要依赖平台 locale——
Windows 默认 GBK，中文技能会乱码或抛异常。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentkit.skills.types import SecretRequirement

logger = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

#: 技能名与 slash 正则同一条（§3.1/§3.5）。
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_TOOL_ALIASES = {
    "Bash": "bash",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "str_replace",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
}
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SkillParseError(ValueError):
    """单个技能文件不可用。storage 决定是跳过它还是致命（§3.4）。"""


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    name: str
    description: str
    license: str | None
    argument_hint: str | None
    allowed_tools: tuple[str, ...] | None
    required_secrets: tuple[SecretRequirement, ...]
    secrets_autonomous: bool
    body: str


def parse_skill_file(path: Path) -> ParsedSkill:
    return parse_skill_text(path.read_text(encoding="utf-8"))  # ★ 显式 UTF-8（§3.1）


def strip_frontmatter(text: str) -> str:
    """Return the body without the YAML frontmatter block."""
    match = FRONTMATTER_RE.match(text)
    return (text[match.end() :] if match is not None else text).strip()


def parse_skill_text(text: str) -> ParsedSkill:
    """同一套解析吃字符串：DurableContext 从工具结果里认出 SKILL.md 时也走这里（§3.10）。"""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillParseError("missing frontmatter")
    front = yaml.safe_load(match.group(1)) or {}
    if not isinstance(front, dict):
        raise SkillParseError("frontmatter must be a mapping")
    raw_name = front.get("name")
    if isinstance(raw_name, bool):
        # YAML 把裸 on/off/yes/no 读成布尔：技能名会被悄悄变成 "True"/"False"
        raise SkillParseError(
            f"bad skill name: {raw_name!r} (quote bare on/off/yes/no in frontmatter)"
        )
    name = str(raw_name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise SkillParseError(f"bad skill name: {name!r}")
    return ParsedSkill(
        name=name,
        description=str(front.get("description") or "").strip(),
        license=_optional_text(front.get("license")),
        argument_hint=_optional_text(front.get("argument-hint")),
        allowed_tools=parse_allowed_tools(front.get("allowed-tools")),
        required_secrets=parse_required_secrets(front.get("required-secrets")),
        secrets_autonomous=_parse_secrets_autonomous(front),
        body=text[match.end() :],
    )


def parse_allowed_tools(raw: Any) -> tuple[str, ...] | None:
    """None = 未声明；() = 声明了空列表。**这两种语义不同**（§3.7），不能都归一成空。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        tokens = _split_tool_tokens(raw)  # 保住空格 / 引号 / 转义括号；未闭合就抛
    elif isinstance(raw, list):
        tokens = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raise SkillParseError(f"allowed-tools must be a string or a list, got {type(raw).__name__}")
    # 已知别名换成运行时名；带参数的条目（Bash(git *)）不在别名表里，原样保留且不生效。
    return tuple(_TOOL_ALIASES.get(token, token) for token in tokens)


def parse_required_secrets(raw: Any) -> tuple[SecretRequirement, ...]:
    """字符串列表或 {name, optional} 映射。**畸形条目丢掉并 warning，不让整个技能失败。**"""
    items: list[SecretRequirement] = []
    if raw is None:
        return ()
    for entry in raw if isinstance(raw, list) else [raw]:
        if isinstance(entry, str):
            name: Any = entry
            optional = False
        elif isinstance(entry, dict):
            name = entry.get("name")
            optional = bool(entry.get("optional", False))
        else:
            name = None
            optional = False
        if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
            logger.warning("Dropping malformed required-secrets entry: %r", entry)
            continue
        items.append(SecretRequirement(name=name, optional=optional))
    return tuple(items)


def _parse_secrets_autonomous(front: dict[str, Any]) -> bool:
    """只有显式 false 才关；键不存在用默认 true；**畸形值 fail-closed 到 false**（§3.3）。"""
    if "secrets-autonomous" not in front:
        return True
    raw = front["secrets-autonomous"]
    if isinstance(raw, bool):
        return raw
    logger.warning("Malformed secrets-autonomous %r; failing closed", raw)
    return False


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_tool_tokens(raw: str) -> list[str]:
    """按空白切分，保住引号与括号里的内容；未闭合的引号/括号直接抛。

    不静默截断：半截的括号声明如果被当成一个普通 token，限制会变成"允许一个永远
    不存在的工具名"——看起来像生效了，其实只是把技能静默废掉。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    escaped = False
    for char in raw:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and (quote is not None or depth > 0):
            current.append(char)
            escaped = True
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            if depth == 0:
                raise SkillParseError(f"unbalanced ')' in allowed-tools: {raw!r}")
            depth -= 1
            current.append(char)
            continue
        if char.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if quote is not None:
        raise SkillParseError(f"unclosed quote in allowed-tools: {raw!r}")
    if depth != 0:
        raise SkillParseError(f"unclosed '(' in allowed-tools: {raw!r}")
    if current:
        tokens.append("".join(current))
    return tokens
