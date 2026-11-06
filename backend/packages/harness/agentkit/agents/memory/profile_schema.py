"""Single source of truth for the user-profile schema.

One list of :class:`ProfileFieldSpec` drives three consumers: value validation,
the `GET /api/memory/profile/schema` HTTP surface that renders the profile form,
and the `update_user_profile` model tool description. Adding or renaming a field
means editing this module only.

Profile fields are deliberately small and stable. Values are free text (or a
free-form string list); the "常见" examples in descriptions are friendly hints,
never an allow-list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

ProfileFieldKind = Literal["text", "string_list"]
ProfileFieldGroup = Literal[
    "identity",
    "communication",
    "work",
]

#: Fields the assistant may only record on an explicit, first-person request.
EXPLICIT_ONLY_FIELDS = frozenset({
    "identity.preferred_name",
})

#: A language preference normally holds a BCP-47 tag; `auto` means "follow the
#: language of the current conversation". Both stay valid free-text values.
FOLLOW_CONVERSATION_LANGUAGE = "auto"

GROUP_LABELS: dict[ProfileFieldGroup, str] = {
    "identity": "身份",
    "communication": "沟通",
    "work": "工作",
}

SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)


@dataclass(frozen=True, slots=True)
class ProfileFieldSpec:
    path: str
    label: str
    group: ProfileFieldGroup
    kind: ProfileFieldKind
    description: str
    placeholder: str = ""
    max_length: int = 256
    max_items: int = 10
    item_max_length: int = 64

    @property
    def group_label(self) -> str:
        return GROUP_LABELS[self.group]

    @property
    def explicit_only(self) -> bool:
        return self.path in EXPLICIT_ONLY_FIELDS


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
        kind="text",
        description="用户所在的地区或语言区域，可自由填写",
        placeholder="例如：杭州、zh-CN",
        max_length=64,
    ),
    ProfileFieldSpec(
        path="communication.language",
        label="语言偏好",
        group="communication",
        kind="text",
        description=(
            "助手回复使用的语言，可自由填写；常见：zh-CN（简体中文）、"
            "en（English）、auto（跟随当前对话）"
        ),
        placeholder="例如：zh-CN 或 auto",
        max_length=64,
    ),
    ProfileFieldSpec(
        path="communication.tone",
        label="偏好语气",
        group="communication",
        kind="text",
        description="助手回复的语气，可自由填写；常见：中性、友好、正式、直接",
        placeholder="例如：中性、直接",
    ),
    ProfileFieldSpec(
        path="communication.verbosity",
        label="详细程度",
        group="communication",
        kind="text",
        description="回答的篇幅，可自由填写；常见：简洁、均衡、详细",
        placeholder="例如：简洁",
    ),
    ProfileFieldSpec(
        path="communication.format",
        label="偏好格式",
        group="communication",
        kind="string_list",
        description=(
            "回答优先使用的结构，可自由填写；"
            "常见：要点、表格、分步、段落、代码优先"
        ),
        placeholder="例如：要点、表格",
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
        description="用户主要涉及的领域或业务方向",
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
        kind="text",
        description="讲解深度，可自由填写；常见：入门、进阶、专家",
        placeholder="例如：进阶",
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
    if looks_like_secret(text):
        raise ValueError(f"{spec.path} looks like a credential and must not be stored")
    if "[REDACTED]" in text or "[[PII:" in text:
        raise ValueError(f"{spec.path} must not contain a redaction marker")
    return text


def _validate_list(spec: ProfileFieldSpec, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{spec.path} must be a list")
    if not value:
        raise ValueError(f"{spec.path} must be a non-empty list")
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
    """Validate one bounded profile field. Values are free text, never notes."""

    spec = PROFILE_FIELD_BY_PATH.get(path)
    if spec is None:
        raise ValueError(f"unknown profile field: {path}")
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
        if spec.kind == "string_list":
            detail += (
                f"；字符串数组，最多 {spec.max_items} 项，"
                f"每项 <= {spec.item_max_length} 字"
            )
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
    "ProfileFieldSpec",
    "SENSITIVE_VALUE_PATTERNS",
    "field_descriptions",
    "looks_like_secret",
    "validate_profile_value",
]
