"""DB-backed profile and local personalization documents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentkit.agents.memory.openviking.contracts import (
    PersonalizationDocument,
    PersonalizationView,
    ProfileView,
)
from agentkit.agents.memory.openviking.models import MemoryProfileDocumentRow
from agentkit.agents.memory.types import (
    FOLLOW_CONVERSATION_LANGUAGE,
    PROFILE_FIELD_PRIORITY,
    validate_profile_value,
)


class ProfileConflict(Exception):
    pass


_FIELD_TO_KEY = {
    "identity.preferred_name": "preferred_name",
    "identity.locale": "locale",
    "communication.language": "language",
    "communication.tone": "tone",
    "communication.verbosity": "verbosity",
    "communication.format": "format",
    "work.role": "role",
    "work.domains": "domains",
    "work.tools": "tools",
    "work.expertise": "expertise",
}
_KEY_TO_FIELD = {key: field for field, key in _FIELD_TO_KEY.items()}
_EMPTY_BODY = "# 用户画像\n\n> 用户身份（profile）。助手身份在 identity.md，助手原则在 soul.md。\n"
_PROFILE_LABELS = {
    "identity.preferred_name": "希望被称呼为",
    "identity.locale": "地区",
    "communication.language": "语言偏好",
    "communication.tone": "偏好语气",
    "communication.verbosity": "详细程度",
    "communication.format": "偏好格式",
    "work.role": "工作角色",
    "work.domains": "工作领域",
    "work.tools": "常用工具",
    "work.expertise": "专业水平",
}


@dataclass(frozen=True, slots=True)
class RenderedProfile:
    text: str
    token_count: int
    truncated: bool


def render_profile_view(view: PersonalizationView, *, max_tokens: int = 1_800) -> RenderedProfile:
    """Render only stable profile fields, in priority order, within a budget."""

    return render_profile_fields(view.profile_fields, max_tokens=max_tokens)


def render_profile_prompt(view: ProfileView, *, max_tokens: int = 1_800) -> str:
    """Render the DB profile as the system-prompt user_profile section."""

    rendered = render_profile_fields(view.fields, max_tokens=max_tokens)
    if not rendered.text:
        return ""
    return (
        '<user_profile source="memory_profile_documents" etag="'
        + escape(view.etag, quote=True)
        + '">\n'
        + escape(rendered.text, quote=False)
        + "\n</user_profile>"
    )


def render_profile_fields(
    fields: dict[str, Any], *, max_tokens: int = 1_800
) -> RenderedProfile:
    """Render profile values as labelled plain text within a character budget."""

    budget = max(1, int(max_tokens)) * 4
    lines: list[str] = []
    used = 0
    truncated = False
    for path in PROFILE_FIELD_PRIORITY:
        value = fields.get(path)
        if value in (None, "", []):
            continue
        if path == "communication.language" and value == FOLLOW_CONVERSATION_LANGUAGE:
            rendered = "跟随当前对话语言"
        elif isinstance(value, (list, tuple)):
            rendered = "、".join(str(item) for item in value if str(item).strip())
        else:
            rendered = str(value).strip()
        if not rendered:
            continue
        line = f"- {_PROFILE_LABELS.get(path, path)}：{rendered}"
        if used + len(line) + 1 > budget:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    text = "\n".join(lines)
    return RenderedProfile(
        text=text,
        token_count=max(1, (len(text) + 3) // 4) if text else 0,
        truncated=truncated,
    )


class ProfileService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        seed_path: str | Path = "config/personalization/profile.md",
    ) -> None:
        self._session_factory = session_factory
        self._seed_path = Path(seed_path)

    async def get_snapshot(self, user_id: str) -> ProfileView:
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileDocumentRow, user_id)
            if row is None:
                await self._insert_seed(session, user_id)
                row = await session.get(MemoryProfileDocumentRow, user_id)
            if row is None:  # pragma: no cover - defensive, insert is atomic
                raise RuntimeError("profile row disappeared during first read")
            return _view(row)

    async def patch(self, user_id: str, fields: dict[str, Any], *, etag: str) -> ProfileView:
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileDocumentRow, user_id)
            if row is None:
                row = await self._ensure_row(session, user_id)
            if row.etag != etag:
                raise ProfileConflict("profile etag conflict")
            current = _parse_fields(row.content)
            for path, value in fields.items():
                if path not in _FIELD_TO_KEY:
                    raise ValueError(f"unknown profile field: {path}")
                if value is None:
                    current.pop(path, None)
                else:
                    current[path] = validate_profile_value(path, value)
            content = _render_content(current)
            await _replace_content(session, user_id, etag, content)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def update_fields(
        self, user_id: str, fields: dict[str, Any], *, attempts: int = 2
    ) -> ProfileView:
        """Apply fields without the caller holding an etag.

        The model tool does not know the current etag, so it reads one and retries
        once against a fresh one. A second conflict is real contention and must
        surface instead of blindly overwriting the user's form submission.
        """

        if not fields:
            return await self.get_snapshot(user_id)
        last: ProfileConflict | None = None
        for _ in range(max(1, int(attempts))):
            snapshot = await self.get_snapshot(user_id)
            try:
                return await self.patch(user_id, fields, etag=snapshot.etag)
            except ProfileConflict as exc:
                last = exc
        raise last or ProfileConflict("profile etag conflict")

    async def delete_field(self, user_id: str, field_path: str, *, etag: str) -> ProfileView:
        if field_path not in _FIELD_TO_KEY:
            raise ValueError(f"unknown profile field: {field_path}")
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileDocumentRow, user_id)
            if row is None:
                row = await self._ensure_row(session, user_id)
            if row.etag != etag:
                raise ProfileConflict("profile etag conflict")
            fields = _parse_fields(row.content)
            fields.pop(field_path, None)
            content = _render_content(fields)
            await _replace_content(session, user_id, etag, content)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def clear(self, user_id: str, *, etag: str) -> ProfileView:
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileDocumentRow, user_id)
            if row is None:
                row = await self._ensure_row(session, user_id)
            if row.etag != etag:
                raise ProfileConflict("profile etag conflict")
            content = _render_content({})
            await _replace_content(session, user_id, etag, content)
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def _ensure_row(
        self, session: AsyncSession, user_id: str
    ) -> MemoryProfileDocumentRow:
        row = await session.get(MemoryProfileDocumentRow, user_id)
        if row is not None:
            return row
        await self._insert_seed(session, user_id)
        row = await session.get(MemoryProfileDocumentRow, user_id)
        if row is None:  # pragma: no cover - defensive, insert is atomic
            raise RuntimeError("profile row disappeared while creating it")
        return row

    async def _insert_seed(self, session: AsyncSession, user_id: str) -> None:
        """Create the first profile row without racing a concurrent GET.

        `SELECT` followed by `INSERT` is a duplicate-key race on a user's
        first profile read. `ON CONFLICT DO NOTHING` makes the winner
        irrelevant and keeps GET idempotent, as the profile contract requires.
        """

        content = self._seed_content()
        now = _now()
        values = {
            "user_id": user_id,
            "schema_version": 1,
            "content": content,
            "etag": _etag(content),
            "created_at": now,
            "updated_at": now,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            statement = insert(MemoryProfileDocumentRow).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["user_id"])
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            statement = insert(MemoryProfileDocumentRow).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["user_id"])
        else:  # pragma: no cover - supported deployments are SQLite/PostgreSQL
            from sqlalchemy import insert

            statement = insert(MemoryProfileDocumentRow).values(**values)
        await session.execute(statement)
        await session.commit()

    def _seed_content(self) -> str:
        try:
            raw = self._seed_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        # Seed frontmatter is parsed and re-rendered so the first etag is the
        # canonical etag, not a hash of arbitrary whitespace in the template.
        return _render_content(_parse_fields(raw))


class PersonalizationService:
    def __init__(
        self,
        profile: ProfileService,
        *,
        identity_path: str | Path,
        soul_path: str | Path,
    ) -> None:
        self._profile = profile
        self._identity_path = Path(identity_path)
        self._soul_path = Path(soul_path)

    async def render(self, user_id: str) -> PersonalizationView:
        snapshot = await self._profile.get_snapshot(user_id)
        identity = _read_document(self._identity_path)
        soul = _read_document(self._soul_path)
        digest = hashlib.sha256(
            f"{identity.sha256}:{soul.sha256}:{snapshot.etag}".encode("utf-8")
        ).hexdigest()
        return PersonalizationView(
            profile_fields=snapshot.fields,
            identity=identity,
            soul=soul,
            profile_etag=snapshot.etag,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class AssistantPersonalization:
    identity: PersonalizationDocument
    soul: PersonalizationDocument
    digest: str


def read_assistant_personalization(
    *,
    identity_path: str | Path,
    soul_path: str | Path,
    max_chars: int = 8_192,
) -> AssistantPersonalization:
    identity = _read_document(Path(identity_path), max_chars=max_chars)
    soul = _read_document(Path(soul_path), max_chars=max_chars)
    missing = f"{int(not identity.content)}:{int(not soul.content)}"
    digest = hashlib.sha256(
        f"{identity.sha256}:{soul.sha256}:{missing}".encode("utf-8")
    ).hexdigest()
    return AssistantPersonalization(identity=identity, soul=soul, digest=digest)


def render_assistant_personalization(value: AssistantPersonalization) -> str:
    sections: list[str] = []
    if value.identity.content.strip():
        sections.append(
            '<assistant-identity source="' + escape(value.identity.source, quote=True) + '">\n'
            + escape(value.identity.content.strip(), quote=False)
            + "\n</assistant-identity>"
        )
    if value.soul.content.strip():
        sections.append(
            '<assistant-soul source="' + escape(value.soul.source, quote=True) + '">\n'
            + escape(value.soul.content.strip(), quote=False)
            + "\n</assistant-soul>"
        )
    return "\n\n".join(sections)


async def _replace_content(
    session: AsyncSession,
    user_id: str,
    expected_etag: str,
    content: str,
) -> None:
    """Compare-and-swap the row; a stale etag must lose atomically."""

    statement = (
        update(MemoryProfileDocumentRow)
        .where(
            MemoryProfileDocumentRow.user_id == user_id,
            MemoryProfileDocumentRow.etag == expected_etag,
        )
        .values(etag=_etag(content), content=content, updated_at=_now())
    )
    result = await session.execute(statement)
    if result.rowcount != 1:
        raise ProfileConflict("profile etag conflict")


def _parse_fields(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        raw = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        path = _KEY_TO_FIELD.get(str(key))
        if path is not None and value not in (None, "", []):
            fields[path] = value
    return fields


def _render_content(fields: dict[str, Any]) -> str:
    raw: dict[str, Any] = {}
    for path in PROFILE_FIELD_PRIORITY:
        if path not in fields:
            continue
        value = fields[path]
        if value in (None, "", []):
            continue
        raw[_FIELD_TO_KEY[path]] = value
    frontmatter = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{_EMPTY_BODY}"


def _etag(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _view(row: MemoryProfileDocumentRow) -> ProfileView:
    return ProfileView(
        fields=_parse_fields(row.content),
        etag=row.etag,
        updated_at=row.updated_at.isoformat(),
    )


def _read_document(path: Path, *, max_chars: int | None = None) -> PersonalizationDocument:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    if max_chars is not None and len(content) > max_chars:
        content = content[:max_chars]
    return PersonalizationDocument(
        content=content,
        source=str(path),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _now() -> datetime:
    return datetime.now(UTC)
