"""The only prompt formatter for long-term memory (M10 section 2.3.2 / 2.8.1)."""

from __future__ import annotations

from dataclasses import dataclass

from agentkit.agents.memory.tokens import count_tokens
from agentkit.agents.memory.types import (
    PROFILE_FIELD_PRIORITY,
    ProfileSnapshot,
    UserProfile,
    is_resident_source,
    profile_field,
)

PROFILE_OPEN = "<user_profile>"
PROFILE_CLOSE = "</user_profile>"

_FIELD_LABELS = {
    "identity.preferred_name": "称呼",
    "identity.pronouns": "代词",
    "identity.locale": "地区",
    "identity.timezone": "时区",
    "communication.language": "语言",
    "communication.tone": "语气",
    "communication.verbosity": "详细程度",
    "communication.format": "格式",
    "work.role": "角色",
    "work.domains": "领域",
    "work.tools": "工具",
    "work.expertise": "专长",
    "accessibility.needs": "无障碍需求",
    "boundaries": "边界",
}


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    text: str
    token_count: int
    truncated: bool
    field_paths: tuple[str, ...]


class ProfileContextBuilder:
    """Render resident profile fields within a whole-field token budget."""

    def __init__(self, *, max_tokens: int = 800, token_counting: str = "char") -> None:
        self.max_tokens = max_tokens
        self.token_counting = token_counting

    def build(self, profile_or_snapshot: UserProfile | ProfileSnapshot | None) -> RenderedProfile:
        if profile_or_snapshot is None:
            return RenderedProfile("", 0, False, ())
        profile = (
            profile_or_snapshot.profile
            if isinstance(profile_or_snapshot, ProfileSnapshot)
            else profile_or_snapshot
        )
        lines: list[tuple[str, str]] = []
        for path in PROFILE_FIELD_PRIORITY:
            field = profile_field(profile, path)
            if field is None:
                continue
            if not is_resident_source(field.source):
                continue
            rendered = _render_value(field.value)
            if rendered is None:
                continue
            lines.append((path, f"{_FIELD_LABELS[path]}: {rendered}"))

        kept: list[tuple[str, str]] = []
        truncated = False
        for path, line in lines:
            candidate = _wrap([text for _, text in [*kept, (path, line)]])
            if count_tokens(candidate, mode=self.token_counting) > self.max_tokens:
                truncated = True
                continue
            kept.append((path, line))
        text = _wrap([line for _, line in kept])
        return RenderedProfile(
            text=text,
            token_count=count_tokens(text, mode=self.token_counting),
            truncated=truncated,
            field_paths=tuple(path for path, _ in kept),
        )


def _wrap(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join([PROFILE_OPEN, *lines, PROFILE_CLOSE])


def _render_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        return cleaned or None
    if isinstance(value, (list, tuple)):
        parts = [" ".join(str(item).split()) for item in value if str(item).strip()]
        return ", ".join(parts) or None
    cleaned = " ".join(str(value).split())
    return cleaned or None
