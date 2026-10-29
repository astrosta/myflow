"""DB-backed profile and local personalization documents."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentkit.agents.memory.openviking.contracts import (
    PersonalizationDocument,
    PersonalizationView,
    ProfileView,
)
from agentkit.agents.memory.openviking.models import MemoryProfileDocumentRow
from agentkit.agents.memory.types import PROFILE_FIELD_PRIORITY, validate_profile_value


class ProfileConflict(Exception):
    pass


_FIELD_TO_KEY = {
    "identity.preferred_name": "preferred_name",
    "identity.pronouns": "pronouns",
    "identity.locale": "locale",
    "identity.timezone": "timezone",
    "communication.language": "language",
    "communication.tone": "tone",
    "communication.verbosity": "verbosity",
    "communication.format": "format",
    "work.role": "role",
    "work.domains": "domains",
    "work.tools": "tools",
    "work.expertise": "expertise",
    "accessibility.needs": "accessibility_needs",
    "boundaries": "boundaries",
}
_KEY_TO_FIELD = {key: field for field, key in _FIELD_TO_KEY.items()}
_EMPTY_BODY = "# 用户画像\n\n> 用户身份（profile）。助手身份在 identity.md，助手原则在 soul.md。\n"


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
                content = self._seed_content()
                row = MemoryProfileDocumentRow(
                    user_id=user_id,
                    schema_version=1,
                    content=content,
                    etag=_etag(content),
                    created_at=_now(),
                    updated_at=_now(),
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
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
            row.content = content
            row.etag = _etag(content)
            row.updated_at = _now()
            await session.commit()
            await session.refresh(row)
            return _view(row)

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
            row.content = content
            row.etag = _etag(content)
            row.updated_at = _now()
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
            row.content = content
            row.etag = _etag(content)
            row.updated_at = _now()
            await session.commit()
            await session.refresh(row)
            return _view(row)

    async def _ensure_row(
        self, session: AsyncSession, user_id: str
    ) -> MemoryProfileDocumentRow:
        row = await session.get(MemoryProfileDocumentRow, user_id)
        if row is not None:
            return row
        content = self._seed_content()
        row = MemoryProfileDocumentRow(
            user_id=user_id,
            schema_version=1,
            content=content,
            etag=_etag(content),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(row)
        await session.flush()
        return row

    def _seed_content(self) -> str:
        try:
            return self._seed_path.read_text(encoding="utf-8")
        except OSError:
            return _render_content({})


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


def _read_document(path: Path) -> PersonalizationDocument:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    return PersonalizationDocument(
        content=content,
        source=str(path),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _now() -> datetime:
    return datetime.now(UTC)
