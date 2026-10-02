"""Declarative base and the one row-to-dict conversion repositories share."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def iso_utc(value: datetime | None) -> str | None:
    """SQLite 读回来是 naive 的（即便列写了 timezone=True）；按 UTC 补 tz 再序列化。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    """Current UTC time in the same form ``iso_utc`` writes for stored timestamps."""
    stamped = iso_utc(datetime.now(UTC))
    if stamped is None:
        raise RuntimeError("iso_utc refused the current time")
    return stamped


_JSON_COLUMN_ALIASES = {
    "metadata_json": "metadata",
    "kwargs_json": "kwargs",
    "event_metadata": "metadata",
}


def row_to_dict(row: Base, *, aliases: Mapping[str, str] = _JSON_COLUMN_ALIASES) -> dict[str, Any]:
    """ORM row to a plain dict. Timestamps are UTC ISO-8601 ending in Z."""
    out: dict[str, Any] = {}
    for column in sa_inspect(type(row)).columns:
        name = aliases.get(column.key, column.key)
        value = getattr(row, column.key)
        out[name] = iso_utc(value) if isinstance(value, datetime) else value
    return out
