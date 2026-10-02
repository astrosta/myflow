"""§3.3 repository conventions: AUTO user id, row dicts, backend-hiding factory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentkit.persistence.base import iso_utc, row_to_dict
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.persistence.thread_meta.base import ThreadMetaStore
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.thread_meta.sql import SqlThreadMetaStore
from agentkit.runtime.user_context import (
    AUTO,
    MissingUserContextError,
    reset_current_user,
    resolve_user_id,
    set_current_user,
)


class _Rows(DeclarativeBase):
    pass


class _SampleRow(_Rows):
    __tablename__ = "sample_row_to_dict"

    id: Mapped[int] = mapped_column(primary_key=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kwargs_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)


class _EventRow(_Rows):
    __tablename__ = "sample_event_row"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)


@pytest.fixture(autouse=True)
def _reset_user_context():
    token = set_current_user(None)
    yield
    reset_current_user(token)


def test_auto_without_a_current_user_fails_closed() -> None:
    with pytest.raises(MissingUserContextError, match=r"ThreadMetaStore\.get") as raised:
        resolve_user_id(AUTO, method_name="ThreadMetaStore.get")
    assert "record.user_id" in str(raised.value)


def test_auto_reads_the_current_user() -> None:
    token = set_current_user("alice")
    try:
        assert resolve_user_id(AUTO, method_name="ThreadMetaStore.get") == "alice"
    finally:
        reset_current_user(token)


def test_explicit_user_id_ignores_the_current_user() -> None:
    token = set_current_user("alice")
    try:
        assert resolve_user_id("bob", method_name="ThreadMetaStore.get") == "bob"
    finally:
        reset_current_user(token)


def test_explicit_none_is_a_global_query() -> None:
    token = set_current_user("alice")
    try:
        assert resolve_user_id(None, method_name="ThreadMetaStore.get") is None
    finally:
        reset_current_user(token)


def test_reset_restores_the_previous_user() -> None:
    outer = set_current_user("alice")
    inner = set_current_user("bob")
    assert resolve_user_id(AUTO, method_name="ThreadMetaStore.get") == "bob"
    reset_current_user(inner)
    assert resolve_user_id(AUTO, method_name="ThreadMetaStore.get") == "alice"
    reset_current_user(outer)
    with pytest.raises(MissingUserContextError):
        resolve_user_id(AUTO, method_name="ThreadMetaStore.get")


def test_auto_repr() -> None:
    assert repr(AUTO) == "AUTO"


def test_naive_datetime_is_serialized_as_utc_z() -> None:
    assert iso_utc(None) is None
    assert iso_utc(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"


def test_aware_datetime_is_converted_to_utc_z() -> None:
    shanghai = timezone(timedelta(hours=8))
    value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=shanghai)
    assert iso_utc(value) == "2026-01-01T19:04:05Z"


def test_row_to_dict_renames_json_columns_and_formats_timestamps() -> None:
    row = _SampleRow(
        id=7,
        metadata_json={"title": "中文"},
        kwargs_json={"temperature": 0.2},
        created_at=datetime(2026, 1, 2, 3, 4, 5),
        lease_expires_at=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        status="idle",
    )
    assert row_to_dict(row) == {
        "id": 7,
        "metadata": {"title": "中文"},
        "kwargs": {"temperature": 0.2},
        "created_at": "2026-01-02T03:04:05Z",
        "lease_expires_at": "2026-06-01T00:00:00Z",
        "status": "idle",
    }
    event = _EventRow(id=1, event_metadata={"source": "journal"})
    assert row_to_dict(event) == {"id": 1, "metadata": {"source": "journal"}}


def test_make_thread_meta_store_hides_the_backend() -> None:
    memory = make_thread_meta_store(None)
    assert isinstance(memory, MemoryThreadMetaStore)
    assert isinstance(memory, ThreadMetaStore)

    factory = object()
    sql = make_thread_meta_store(factory)
    assert isinstance(sql, SqlThreadMetaStore)
    assert isinstance(sql, ThreadMetaStore)
    assert sql._sf is factory
