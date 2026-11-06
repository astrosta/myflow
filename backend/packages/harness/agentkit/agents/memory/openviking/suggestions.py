"""Profile suggestions generated from OpenViking memory.

Suggestions are proposals only. Accepting one is the sole path that writes the
profile; generation never mutates the profile and never turns a memory into an
instruction. The deterministic rules cover clear statements; ambiguous prose
simply produces no suggestion instead of guessing.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentkit.agents.memory.openviking.contracts import MemoryHit
from agentkit.agents.memory.openviking.models import MemoryProfileSuggestionRow
from agentkit.agents.memory.openviking.profile import ProfileConflict, ProfileService
from agentkit.agents.memory.types import validate_profile_value
from agentkit.privacy import PIIRedactor

_STATUSES = frozenset({"pending", "accepted", "dismissed", "expired"})
_EMPTY = object()


class SuggestionNotFound(Exception):
    pass


class SuggestionConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProfileSuggestion:
    id: str
    field_path: str
    value: Any
    evidence_uri: str
    evidence_quote: str
    confidence: float
    reason: str
    status: str
    profile_etag: str
    expires_at: str

    @property
    def value_hash(self) -> str:
        return _value_hash(self.value)


class ProfileSuggestionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def expire(self, user_id: str) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            await session.execute(
                update(MemoryProfileSuggestionRow)
                .where(
                    MemoryProfileSuggestionRow.user_id == user_id,
                    MemoryProfileSuggestionRow.status == "pending",
                    MemoryProfileSuggestionRow.expires_at <= now,
                )
                .values(status="expired", acted_at=now)
            )
            await session.commit()

    async def list_pending(self, user_id: str, profile_etag: str) -> list[ProfileSuggestion]:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MemoryProfileSuggestionRow)
                    .where(
                        MemoryProfileSuggestionRow.user_id == user_id,
                        MemoryProfileSuggestionRow.status == "pending",
                        MemoryProfileSuggestionRow.profile_etag == profile_etag,
                        MemoryProfileSuggestionRow.expires_at > now,
                    )
                    .order_by(MemoryProfileSuggestionRow.created_at)
                )
            ).all()
            return [_from_row(row) for row in rows]

    async def blocked_keys(self, user_id: str) -> set[tuple[str, str]]:
        """Dismissed values stay hidden for their remaining TTL."""

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        MemoryProfileSuggestionRow.field_path,
                        MemoryProfileSuggestionRow.value_hash,
                    ).where(
                        MemoryProfileSuggestionRow.user_id == user_id,
                        MemoryProfileSuggestionRow.status == "dismissed",
                        MemoryProfileSuggestionRow.expires_at > now,
                    )
                )
            ).all()
            return {(str(field), str(value_hash)) for field, value_hash in rows}

    async def get(self, user_id: str, suggestion_id: str) -> ProfileSuggestion:
        async with self._session_factory() as session:
            row = await session.get(
                MemoryProfileSuggestionRow, (user_id, suggestion_id)
            )
            if row is None:
                raise SuggestionNotFound(suggestion_id)
            return _from_row(row)

    async def insert_if_missing(
        self, user_id: str, suggestion: ProfileSuggestion
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "user_id": user_id,
            "suggestion_id": suggestion.id,
            "field_path": suggestion.field_path,
            "value_json": json.dumps(suggestion.value, ensure_ascii=False, sort_keys=True),
            "value_hash": suggestion.value_hash,
            "evidence_uri": suggestion.evidence_uri,
            "evidence_quote": suggestion.evidence_quote,
            "confidence": suggestion.confidence,
            "reason": suggestion.reason,
            "profile_etag": suggestion.profile_etag,
            "status": "pending",
            "created_at": now,
            "expires_at": _parse_time(suggestion.expires_at),
        }
        async with self._session_factory() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            elif dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            else:  # pragma: no cover
                dialect_insert = insert
            statement = dialect_insert(MemoryProfileSuggestionRow).values(**values)
            if dialect in {"sqlite", "postgresql"}:
                statement = statement.on_conflict_do_nothing()
            await session.execute(statement)
            await session.commit()

    async def mark(self, user_id: str, suggestion_id: str, status: str) -> None:
        if status not in _STATUSES:
            raise ValueError(f"invalid suggestion status: {status}")
        async with self._session_factory() as session:
            result = await session.execute(
                update(MemoryProfileSuggestionRow)
                .where(
                    MemoryProfileSuggestionRow.user_id == user_id,
                    MemoryProfileSuggestionRow.suggestion_id == suggestion_id,
                    MemoryProfileSuggestionRow.status == "pending",
                )
                .values(status=status, acted_at=datetime.now(UTC))
            )
            await session.commit()
            if result.rowcount != 1:
                raise SuggestionConflict("suggestion is no longer pending")


class ProfileSuggester:
    def __init__(
        self,
        store: ProfileSuggestionStore,
        profile: ProfileService,
        facade: Any,
        *,
        suggestion_days: int = 7,
    ) -> None:
        self._store = store
        self._profile = profile
        self._facade = facade
        self._suggestion_days = max(1, int(suggestion_days))
        self._redactor = PIIRedactor()

    async def get(self, user_id: str) -> list[ProfileSuggestion]:
        snapshot = await self._profile.get_snapshot(user_id)
        await self._store.expire(user_id)
        existing = await self._store.list_pending(user_id, snapshot.etag)
        if existing:
            return existing
        await self._generate(user_id, snapshot.etag, snapshot.fields)
        return await self._store.list_pending(user_id, snapshot.etag)

    async def accept(
        self, user_id: str, suggestion_id: str, *, etag: str | None = None
    ) -> tuple[ProfileSuggestion, Any]:
        suggestion = await self._store.get(user_id, suggestion_id)
        _ensure_pending(suggestion)
        snapshot = await self._profile.get_snapshot(user_id)
        if etag is not None and etag != snapshot.etag:
            raise SuggestionConflict("profile etag conflict")
        if suggestion.profile_etag != snapshot.etag:
            raise SuggestionConflict("suggestion was generated for an older profile")
        try:
            updated = await self._profile.patch(
                user_id,
                {suggestion.field_path: suggestion.value},
                etag=snapshot.etag,
            )
        except ProfileConflict as exc:
            raise SuggestionConflict("profile etag conflict") from exc
        await self._store.mark(user_id, suggestion_id, "accepted")
        return replace(suggestion, status="accepted"), updated

    async def dismiss(self, user_id: str, suggestion_id: str) -> ProfileSuggestion:
        suggestion = await self._store.get(user_id, suggestion_id)
        _ensure_pending(suggestion)
        await self._store.mark(user_id, suggestion_id, "dismissed")
        return replace(suggestion, status="dismissed")

    async def _generate(
        self,
        user_id: str,
        profile_etag: str,
        current_fields: dict[str, Any],
    ) -> None:
        try:
            hits = await self._facade.search(
                user_id,
                "用户画像偏好",
                memory_types=["preferences", "entities", "events"],
                limit=20,
            )
        except Exception:
            return
        blocked = await self._store.blocked_keys(user_id)
        candidates: dict[tuple[str, str], ProfileSuggestion] = {}
        for hit in hits:
            for field_path, value, reason, confidence in _candidates(hit):
                try:
                    value = validate_profile_value(field_path, value)
                except ValueError:
                    continue
                value_hash = _value_hash(value)
                suggestion_id = _suggestion_id(
                    user_id, field_path, value_hash, hit.uri, profile_etag
                )
                key = (field_path, value_hash)
                if key in blocked or current_fields.get(field_path) == value:
                    continue
                current = candidates.get(key)
                if current is not None and current.confidence >= confidence:
                    continue
                candidates[key] = ProfileSuggestion(
                    id=suggestion_id,
                    field_path=field_path,
                    value=value,
                    evidence_uri=hit.uri,
                    evidence_quote=_quote(hit.abstract, self._redactor),
                    confidence=confidence,
                    reason=reason,
                    status="pending",
                    profile_etag=profile_etag,
                    expires_at=(
                        datetime.now(UTC) + timedelta(days=self._suggestion_days)
                    ).isoformat(),
                )
        for suggestion in candidates.values():
            await self._store.insert_if_missing(user_id, suggestion)


def _ensure_pending(suggestion: ProfileSuggestion) -> None:
    if suggestion.status != "pending":
        raise SuggestionConflict("suggestion is no longer pending")
    try:
        if _parse_time(suggestion.expires_at) <= datetime.now(UTC):
            raise SuggestionConflict("suggestion expired")
    except ValueError as exc:
        raise SuggestionConflict("suggestion has an invalid expiry") from exc


def _from_row(row: MemoryProfileSuggestionRow) -> ProfileSuggestion:
    try:
        value = json.loads(row.value_json)
    except json.JSONDecodeError:
        value = None
    return ProfileSuggestion(
        id=row.suggestion_id,
        field_path=row.field_path,
        value=value,
        evidence_uri=row.evidence_uri,
        evidence_quote=row.evidence_quote,
        confidence=float(row.confidence),
        reason=row.reason,
        status=row.status,
        profile_etag=row.profile_etag,
        expires_at=row.expires_at.isoformat(),
    )


def _candidates(hit: MemoryHit) -> list[tuple[str, Any, str, float]]:
    text = _plain_text(hit.abstract)
    lowered = text.casefold()
    found: list[tuple[str, Any, str, float]] = []
    if not text:
        return found

    name = _match(
        r"(?:叫我|称呼我?为?|希望被称呼为?|我的名字是?)\s*([^\s，。,.！!？?]{1,20})",
        text,
    )
    if name:
        found.append(("identity.preferred_name", name, "用户明确说明称呼", 0.82))

    if "中文" in text or "汉语" in text:
        found.append(("communication.language", "zh-CN", "偏好中文表达", 0.78))
    elif "English" in text or "英文" in lowered:
        found.append(("communication.language", "en-US", "偏好英文表达", 0.78))

    if any(token in text for token in ("简洁", "简短", "简明", "少废话")):
        found.append(("communication.verbosity", "简洁", "偏好简洁输出", 0.74))
    elif any(token in text for token in ("详细", "展开说明", "逐步解释")):
        found.append(("communication.verbosity", "详细", "偏好详细输出", 0.72))

    if any(token in text for token in ("直接", "直白", "别绕")):
        found.append(("communication.tone", "直接", "偏好直接语气", 0.70))
    elif "正式" in text:
        found.append(("communication.tone", "正式", "偏好正式语气", 0.66))
    elif "友好" in text or "亲切" in text:
        found.append(("communication.tone", "友好", "偏好友好语气", 0.62))

    formats = []
    if "表格" in text:
        formats.append("表格")
    if "列表" in text or "要点" in text:
        formats.append("要点")
    if "步骤" in text or "分步" in text:
        formats.append("分步")
    if "代码优先" in text:
        formats.append("代码优先")
    if formats:
        found.append(("communication.format", formats, "偏好输出格式", 0.68))

    locale = _match(r"(?:住在|来自|坐标|所在地)[:：\s]*([\u4e00-\u9fffA-Za-z-]{2,24})", text)
    if locale:
        found.append(("identity.locale", _locale(locale), "用户说明所在地区", 0.66))

    role = _match(r"(?:我是|职业是|担任)[:：\s]*([^，。,.！!？?]{2,40})", text)
    if role:
        found.append(("work.role", role, "用户说明工作角色", 0.64))

    tools = [
        tool
        for tool in ("Python", "Java", "Go", "Rust", "TypeScript", "React", "SQL", "Docker", "Kubernetes")
        if tool.casefold() in lowered
    ]
    if tools:
        found.append(("work.tools", tools, "用户提到常用工具", 0.62))

    domains = [
        domain
        for domain in ("前端", "后端", "数据", "算法", "产品", "设计", "运维", "安全")
        if domain in text
    ]
    if domains:
        found.append(("work.domains", domains, "用户提到工作领域", 0.60))

    return found


def _plain_text(value: str) -> str:
    text = str(value or "")
    if "<memory-safety>" in text:
        text = text.split("<memory-safety>", 1)[0]
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _locale(value: str) -> str:
    return "zh-CN" if any("\u4e00" <= char <= "\u9fff" for char in value) else value


def _quote(value: str, redactor: PIIRedactor) -> str:
    text = redactor.redact_for_memory(_plain_text(value))
    return text[:500]


def _value_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _suggestion_id(
    user_id: str, field_path: str, value_hash: str, uri: str, profile_etag: str
) -> str:
    payload = f"{user_id}\0{field_path}\0{value_hash}\0{uri}\0{profile_etag}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "ProfileSuggester",
    "ProfileSuggestion",
    "ProfileSuggestionStore",
    "SuggestionConflict",
    "SuggestionNotFound",
]
