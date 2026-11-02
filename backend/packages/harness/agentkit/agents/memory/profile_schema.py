"""Single source of truth for the bounded user-profile schema.

One list of :class:`ProfileFieldSpec` drives three consumers: value validation,
the `GET /api/memory/profile/schema` HTTP surface that renders the profile form,
and the `update_user_profile` model tool description. Adding or renaming a field
means editing this module only.

The schema stays deliberately small: profile fields are stable, cross-task,
enumerable and safe to display. Free-form notes belong to memory facts, never
here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

ProfileFieldKind = Literal[
    "text",
    "locale",
    "language",
    "choice",
    "multi_choice",
    "string_list",
]
ProfileFieldGroup = Literal[
    "identity",
    "communication",
    "work",
    "accessibility",
    "boundaries",
]

#: Fields the assistant may only record on an explicit, first-person request.
EXPLICIT_ONLY_FIELDS = frozenset({
    "identity.preferred_name",
    "accessibility.needs",
    "boundaries",
})

#: A language preference normally holds a BCP-47 tag; `auto` means "follow the
#: language of the current conversation".
FOLLOW_CONVERSATION_LANGUAGE = "auto"

GROUP_LABELS: dict[ProfileFieldGroup, str] = {
    "identity": "身份",
    "communication": "沟通",
    "work": "工作",
    "accessibility": "无障碍",
    "boundaries": "边界",
}

SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)

_VALID_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True, slots=True)
class ProfileFieldOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ProfileFieldSpec:
    path: str
    label: str
    group: ProfileFieldGroup
    kind: ProfileFieldKind
    description: str
    placeholder: str = ""
    options: tuple[ProfileFieldOption, ...] = ()
    max_length: int = 256
    max_items: int = 10
    item_max_length: int = 64

    @property
    def group_label(self) -> str:
        return GROUP_LABELS[self.group]

    @property
    def explicit_only(self) -> bool:
        return self.path in EXPLICIT_ONLY_FIELDS

    @property
    def allowed_values(self) -> tuple[str, ...]:
        return tuple(option.value for option in self.options)


PROFILE_FIELD_SPECS: tuple[ProfileFieldSpec, ...] = (
    ProfileFieldSpec(
        path="identity.preferred_name",
        label="称呼",
        group="identity",
        kind="text",
        description="希望被称呼的名字或昵称",
        placeholder="例如：小林",
        max_length=64,
    ),
    ProfileFieldSpec(
        path="identity.locale",
        label="地区",
        group="identity",
        kind="locale",
        description="用户所在的地区/语言区域，BCP-47 标签",
        placeholder="例如：zh-CN",
        max_length=35,
    ),
    ProfileFieldSpec(
        path="communication.language",
        label="语言偏好",
        group="communication",
        kind="language",
        description="助手回复使用的语言；auto 表示跟随当前对话语言",
        options=(
            ProfileFieldOption("zh-CN", "简体中文"),
            ProfileFieldOption("zh-TW", "繁体中文"),
            ProfileFieldOption("en", "English"),
            ProfileFieldOption(FOLLOW_CONVERSATION_LANGUAGE, "跟随当前对话语言"),
        ),
        max_length=35,
    ),
    ProfileFieldSpec(
        path="accessibility.needs",
        label="无障碍需求",
        group="accessibility",
        kind="string_list",
        description="用户显式说明的无障碍需求（助手只按需适配）",
        placeholder="例如：字号调大",
        max_items=10,
        item_max_length=120,
    ),
    ProfileFieldSpec(
        path="boundaries",
        label="边界",
        group="boundaries",
        kind="string_list",
        description="用户明确设定的行为边界（例如不要主动发邮件）",
        placeholder="例如：不要主动发邮件",
        max_items=10,
        item_max_length=200,
    ),
    ProfileFieldSpec(
        path="communication.tone",
        label="偏好语气",
        group="communication",
        kind="choice",
        description="助手回复的语气",
        options=(
            ProfileFieldOption("neutral", "中性"),
            ProfileFieldOption("friendly", "友好"),
            ProfileFieldOption("formal", "正式"),
            ProfileFieldOption("direct", "直接"),
        ),
    ),
    ProfileFieldSpec(
        path="communication.verbosity",
        label="详细程度",
        group="communication",
        kind="choice",
        description="回答的篇幅",
        options=(
            ProfileFieldOption("concise", "简洁"),
            ProfileFieldOption("balanced", "均衡"),
            ProfileFieldOption("detailed", "详细"),
        ),
    ),
    ProfileFieldSpec(
        path="communication.format",
        label="偏好格式",
        group="communication",
        kind="multi_choice",
        description="回答优先使用的结构，可多选",
        options=(
            ProfileFieldOption("bullets", "要点"),
            ProfileFieldOption("tables", "表格"),
            ProfileFieldOption("steps", "分步"),
            ProfileFieldOption("prose", "段落"),
            ProfileFieldOption("code_first", "代码优先"),
        ),
        max_items=5,
    ),
    ProfileFieldSpec(
        path="work.role",
        label="工作角色",
        group="work",
        kind="text",
        description="用户的工作角色或职责",
        placeholder="例如：后端工程师",
        max_length=100,
    ),
    ProfileFieldSpec(
        path="work.domains",
        label="工作领域",
        group="work",
        kind="string_list",
        description="用户主要涉及的领域/业务方向",
        placeholder="例如：支付、风控",
        max_items=10,
        item_max_length=64,
    ),
    ProfileFieldSpec(
        path="work.tools",
        label="常用工具",
        group="work",
        kind="string_list",
        description="用户日常使用的技术栈或工具",
        placeholder="例如：FastAPI、Postgres",
        max_items=20,
        item_max_length=64,
    ),
    ProfileFieldSpec(
        path="work.expertise",
        label="专业水平",
        group="work",
        kind="choice",
        description="讲解深度：入门 / 进阶 / 专家",
        options=(
            ProfileFieldOption("beginner", "入门"),
            ProfileFieldOption("intermediate", "进阶"),
            ProfileFieldOption("expert", "专家"),
        ),
    ),
)

PROFILE_FIELD_BY_PATH: dict[str, ProfileFieldSpec] = {
    spec.path: spec for spec in PROFILE_FIELD_SPECS
}

#: Backwards-compatible name: injection and truncation order is this tuple.
PROFILE_FIELD_PRIORITY: tuple[str, ...] = tuple(
    spec.path for spec in PROFILE_FIELD_SPECS
)
PROFILE_FIELD_PATHS = frozenset(PROFILE_FIELD_PRIORITY)

MAX_LIST_ITEM_CHARS = 200


def _validate_string(spec: ProfileFieldSpec, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{spec.path} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{spec.path} must not be empty")
    if len(text) > spec.max_length:
        raise ValueError(f"{spec.path} must be at most {spec.max_length} characters")
    if spec.kind in {"locale", "language"}:
        if text != FOLLOW_CONVERSATION_LANGUAGE and not _VALID_LOCALE.fullmatch(text):
            raise ValueError(f"{spec.path} must be a BCP-47 language tag")
    if looks_like_secret(text):
        raise ValueError(f"{spec.path} looks like a credential and must not be stored")
    if "[REDACTED]" in text or "[[PII:" in text:
        raise ValueError(f"{spec.path} must not contain a redaction marker")
    return text


def _validate_list(spec: ProfileFieldSpec, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{spec.path} must be a list")
    if len(value) > spec.max_items:
        raise ValueError(f"{spec.path} accepts at most {spec.max_items} items")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{spec.path} items must be non-empty strings")
        if len(item) > spec.item_max_length:
            raise ValueError(
                f"{spec.path} items must be at most {spec.item_max_length} characters"
            )
        text = item.strip()
        if looks_like_secret(text):
            raise ValueError(
                f"{spec.path} looks like a credential and must not be stored"
            )
        if "[REDACTED]" in text or "[[PII:" in text:
            raise ValueError(f"{spec.path} must not contain a redaction marker")
        cleaned.append(text)
    return cleaned


def validate_profile_value(path: str, value: Any) -> Any:
    """Validate one bounded profile field. Values are never free-form notes."""

    spec = PROFILE_FIELD_BY_PATH.get(path)
    if spec is None:
        raise ValueError(f"unknown profile field: {path}")
    if value is None and path in EXPLICIT_ONLY_FIELDS:
        return None
    if spec.kind == "language" and value == FOLLOW_CONVERSATION_LANGUAGE:
        return FOLLOW_CONVERSATION_LANGUAGE
    if spec.kind == "choice":
        if value not in spec.allowed_values:
            raise ValueError(
                f"{spec.path} must be one of: {', '.join(spec.allowed_values)}"
            )
        return value
    if spec.kind == "multi_choice":
        if not isinstance(value, list) or not value or len(value) > len(spec.options):
            raise ValueError(
                f"{spec.path} must be a non-empty list of known values: "
                f"{', '.join(spec.allowed_values)}"
            )
        if any(item not in spec.allowed_values for item in value):
            raise ValueError(
                f"{spec.path} contains an unknown value; allowed: "
                f"{', '.join(spec.allowed_values)}"
            )
        return list(dict.fromkeys(value))
    if spec.kind == "string_list":
        return _validate_list(spec, value)
    return _validate_string(spec, value)


def looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS)


def field_descriptions() -> list[str]:
    """Human-readable field inventory shared by tools and docs."""

    lines: list[str] = []
    for spec in PROFILE_FIELD_SPECS:
        detail = spec.description
        if spec.options:
            detail += "；可选值：" + "、".join(
                f"{option.value}={option.label}" for option in spec.options
            )
        elif spec.kind == "string_list":
            detail += f"；字符串数组，最多 {spec.max_items} 项，每项 <= {spec.item_max_length} 字"
        else:
            detail += f"；字符串，最多 {spec.max_length} 字"
        if spec.explicit_only:
            detail += "；仅在用户明确要求记录时写入"
        lines.append(f"- {spec.path}（{spec.label}）：{detail}")
    return lines


__all__ = [
    "EXPLICIT_ONLY_FIELDS",
    "FOLLOW_CONVERSATION_LANGUAGE",
    "GROUP_LABELS",
    "MAX_LIST_ITEM_CHARS",
    "PROFILE_FIELD_BY_PATH",
    "PROFILE_FIELD_PATHS",
    "PROFILE_FIELD_PRIORITY",
    "PROFILE_FIELD_SPECS",
    "ProfileFieldGroup",
    "ProfileFieldKind",
    "ProfileFieldOption",
    "ProfileFieldSpec",
    "SENSITIVE_VALUE_PATTERNS",
    "field_descriptions",
    "looks_like_secret",
    "validate_profile_value",
]
