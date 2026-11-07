"""Local-time scheduling for the daily ContextForge catalog pass.

The gateway runs one worker, so the fallback full tools/list is a plain process
task: once a day, at contextforge.sync.daily_at in
contextforge.sync.timezone ("local" means the host zone), plus a bounded jitter
so a fleet of restarts does not line up on the same instant.  There is no leader
election and no database lease - the single-worker guard in the app layer is what
makes that honest (rev.9 6.3/6.4).

The arithmetic lives here, away from the runtime, so the timezone and
day-rollover rules are testable without an event loop.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, time, timedelta, tzinfo

#: The config sentinel for "whatever zone this host is in".
LOCAL_TIMEZONE = "local"


def resolve_timezone(value: str | tzinfo | None) -> tzinfo:
    """The tzinfo for contextforge.sync.timezone.

    "local" (the default) resolves to the host's current local zone.  An IANA
    name resolves through zoneinfo, which keeps DST transitions honest; an
    unknown name is a configuration error and raises instead of silently falling
    back to UTC.
    """
    if isinstance(value, tzinfo):
        return value
    text = str(value or "").strip()
    if not text or text.casefold() == LOCAL_TIMEZONE:
        local = datetime.now().astimezone().tzinfo
        if local is None:  # pragma: no cover - every platform defines a local zone
            raise ValueError("the host reports no local timezone")
        return local
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception as exc:  # noqa: BLE001 - any failure means "bad config"
        raise ValueError(f"unknown contextforge.sync.timezone: {text!r}") from exc


def parse_daily_at(value: str) -> time:
    """Parse HH:MM from contextforge.sync.daily_at."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"contextforge.sync.daily_at must be HH:MM, got {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"contextforge.sync.daily_at must be HH:MM, got {text!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"contextforge.sync.daily_at is out of range: {text!r}")
    return time(hour=hour, minute=minute)


def next_daily_run(
    now: datetime,
    timezone: str | tzinfo | None = LOCAL_TIMEZONE,
    daily_at: str = "00:00",
) -> datetime:
    """The next daily_at in timezone strictly after now.

    now must be timezone-aware: a naive datetime would silently compare against
    a different clock than the one the process runs on, which is the class of bug
    this module exists to avoid.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware to schedule a daily run")
    zone = resolve_timezone(timezone)
    at = parse_daily_at(daily_at)
    local_now = now.astimezone(zone)
    target = datetime.combine(local_now.date(), at, tzinfo=zone)
    if target <= local_now:
        target = datetime.combine(local_now.date() + timedelta(days=1), at, tzinfo=zone)
    return target


def next_local_midnight(
    now: datetime, timezone: str | tzinfo | None = LOCAL_TIMEZONE
) -> datetime:
    """The next local 00:00 strictly after now."""
    return next_daily_run(now, timezone, "00:00")


def daily_delay(
    now: datetime,
    *,
    timezone: str | tzinfo | None = LOCAL_TIMEZONE,
    daily_at: str = "00:00",
    jitter_seconds: float = 0.0,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to sleep before the next daily run, jitter included."""
    target = next_daily_run(now, timezone, daily_at)
    spread = max(0.0, float(jitter_seconds))
    return max(0.0, (target - now).total_seconds()) + spread * float(rand())


__all__ = [
    "LOCAL_TIMEZONE",
    "daily_delay",
    "next_daily_run",
    "next_local_midnight",
    "parse_daily_at",
    "resolve_timezone",
]
