"""Profile onboarding state for the OpenViking authority.

Onboarding is a dismissible UI hint, not a login gate and not a chat message.
The row is user-scoped and independent from the profile document.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentkit.agents.memory.openviking.models import MemoryProfileOnboardingRow
from agentkit.agents.memory.openviking.profile import ProfileService
from agentkit.agents.memory.openviking.store import _insert_ignore

OnboardingAction = Literal["seen", "snooze", "dismiss", "complete"]
_ONBOARDING_ACTIONS = frozenset({"seen", "snooze", "dismiss", "complete"})


@dataclass(frozen=True, slots=True)
class OnboardingView:
    status: str
    should_show: bool
    profile_field_count: int
    snooze_until: str | None
    dismissed_at: str | None
    updated_at: str


class OnboardingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        profile: ProfileService,
        *,
        snooze_days: int = 7,
    ) -> None:
        self._session_factory = session_factory
        self._profile = profile
        self._snooze_days = max(1, int(snooze_days))

    async def get(self, user_id: str, *, active_run: bool = False) -> OnboardingView:
        row = await self._get_or_create(user_id)
        snapshot = await self._profile.get_snapshot(user_id)
        field_count = sum(
            1 for value in snapshot.fields.values() if value not in (None, "", [])
        )
        now = datetime.now(UTC)
        snooze_active = (
            row.status == "snoozed"
            and row.snooze_until is not None
            and _aware(row.snooze_until) > now
        )
        should_show = (
            field_count < 2
            and row.status not in {"completed", "dismissed"}
            and not snooze_active
            and not active_run
        )
        if should_show and row.status == "not_started":
            await self._set_status(user_id, "seen", seen_at=now)
            row = await self._get_or_create(user_id)
        return _view(row, should_show, field_count)

    async def update(self, user_id: str, action: str) -> OnboardingView:
        if action not in _ONBOARDING_ACTIONS:
            raise ValueError(f"unknown onboarding action: {action}")
        row = await self._get_or_create(user_id)
        now = datetime.now(UTC)
        values: dict[str, Any] = {"status": action}
        if action == "seen":
            values["seen_at"] = now
        elif action == "snooze":
            values["status"] = "snoozed"
            values["snooze_until"] = now + timedelta(days=self._snooze_days)
        elif action == "dismiss":
            values["status"] = "dismissed"
            values["dismissed_at"] = now
        elif action == "complete":
            values["status"] = "completed"
        async with self._session_factory() as session:
            current = await session.get(MemoryProfileOnboardingRow, user_id)
            if current is None:
                await _insert_ignore(
                    session,
                    MemoryProfileOnboardingRow,
                    {
                        "user_id": user_id,
                        "status": "not_started",
                        "updated_at": now,
                    },
                    index_elements=["user_id"],
                )
                await session.commit()
                current = await session.get(MemoryProfileOnboardingRow, user_id)
            if current is None:  # pragma: no cover - defensive
                raise RuntimeError("onboarding row disappeared")
            for key, value in values.items():
                setattr(current, key, value)
            current.updated_at = now
            await session.commit()
            await session.refresh(current)
        snapshot = await self._profile.get_snapshot(user_id)
        field_count = sum(
            1 for value in snapshot.fields.values() if value not in (None, "", [])
        )
        return _view(current, False, field_count)

    async def _get_or_create(self, user_id: str) -> MemoryProfileOnboardingRow:
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileOnboardingRow, user_id)
            if row is not None:
                return row
            now = datetime.now(UTC)
            await _insert_ignore(
                session,
                MemoryProfileOnboardingRow,
                {"user_id": user_id, "status": "not_started", "updated_at": now},
                index_elements=["user_id"],
            )
            await session.commit()
            row = await session.get(MemoryProfileOnboardingRow, user_id)
            if row is None:  # pragma: no cover - defensive
                raise RuntimeError("onboarding row disappeared during creation")
            return row

    async def _set_status(self, user_id: str, status: str, *, seen_at: datetime) -> None:
        async with self._session_factory() as session:
            row = await session.get(MemoryProfileOnboardingRow, user_id)
            if row is None:
                return
            row.status = status
            row.seen_at = seen_at
            row.updated_at = seen_at
            await session.commit()


def _view(row: MemoryProfileOnboardingRow, should_show: bool, field_count: int) -> OnboardingView:
    return OnboardingView(
        status=row.status,
        should_show=should_show,
        profile_field_count=field_count,
        snooze_until=row.snooze_until.isoformat() if row.snooze_until else None,
        dismissed_at=row.dismissed_at.isoformat() if row.dismissed_at else None,
        updated_at=row.updated_at.isoformat(),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["OnboardingAction", "OnboardingService", "OnboardingView"]
