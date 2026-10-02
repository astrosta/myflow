"""§4.4 ThreadMetaStore memory and SQL implementations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update

from agentkit.persistence.base import iso_utc
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.json_compat import InvalidMetadataFilterError
from agentkit.persistence.thread_meta.base import ThreadMetaStore, ThreadOwnershipConflictError
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.thread_meta.model import ThreadMetaRow
from agentkit.persistence.thread_meta.sql import SqlThreadMetaStore
from agentkit.runtime.user_context import AUTO, MissingUserContextError, reset_current_user, set_current_user


def _run(scenario) -> None:
    async def _body() -> None:
        token = set_current_user(None)
        try:
            await scenario()
        finally:
            reset_current_user(token)
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


async def _open(backend: str, tmp_path: Path) -> ThreadMetaStore:
    if backend == "memory":
        return MemoryThreadMetaStore()
    await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
    factory = get_session_factory()
    assert factory is not None
    return SqlThreadMetaStore(factory)


async def _stamp(store: ThreadMetaStore, thread_id: str, when: datetime) -> None:
    if isinstance(store, MemoryThreadMetaStore):
        store._rows[thread_id]["updated_at"] = iso_utc(when)
        return
    factory = get_session_factory()
    assert factory is not None
    async with factory() as session:
        table = ThreadMetaRow.__table__
        await session.execute(
            update(table).where(table.c.thread_id == thread_id).values(updated_at=when)
        )
        await session.commit()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_create_is_idempotent_for_the_same_user_and_rejects_another(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        with pytest.raises(MissingUserContextError, match=r"ThreadMetaStore\.create"):
            await store.create("thread-1", user_id=AUTO)
        created = await store.create(
            "thread-1",
            assistant_id="assistant-1",
            user_id="alice",
            display_name="",
            metadata={"owner_id": "hidden", "topic": "中文"},
        )
        assert created["user_id"] == "alice"
        assert created["assistant_id"] == "assistant-1"
        assert created["display_name"] is None
        assert created["status"] == "idle"
        assert created["metadata"] == {"topic": "中文"}
        assert len(created["incarnation"]) == 32
        assert created["created_at"].endswith("Z")

        again = await store.create("thread-1", user_id="alice", metadata={"topic": "changed"})
        assert again["incarnation"] == created["incarnation"]
        assert again["metadata"] == {"topic": "中文"}

        with pytest.raises(ThreadOwnershipConflictError):
            await store.create("thread-1", user_id="bob")

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_get_filters_by_owner_unless_the_caller_passes_none(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        await store.create("thread-1", user_id="alice", metadata={"topic": "中文"})
        with pytest.raises(MissingUserContextError, match=r"ThreadMetaStore\.get"):
            await store.get("thread-1", user_id=AUTO)
        assert await store.get("thread-1", user_id="bob") is None
        assert (await store.get("thread-1", user_id="alice"))["metadata"] == {"topic": "中文"}
        assert (await store.get("thread-1", user_id=None))["user_id"] == "alice"
        assert await store.get("missing", user_id=None) is None

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_search_orders_by_updated_at_and_filters_status(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        await store.create("m", user_id="alice")
        await store.create("a", user_id="alice")
        await store.create("b", user_id="bob")
        await store.update_status("a", "running", user_id="alice")
        await _stamp(store, "m", datetime(2026, 1, 1, tzinfo=UTC))
        await _stamp(store, "a", datetime(2026, 1, 3, tzinfo=UTC))
        await _stamp(store, "b", datetime(2026, 1, 2, tzinfo=UTC))

        page = await store.search(user_id="alice", limit=1, offset=0)
        assert [row["thread_id"] for row in page] == ["a"]
        rest = await store.search(user_id=None, status="idle", limit=10, offset=0)
        assert [row["thread_id"] for row in rest] == ["b", "m"]
        with pytest.raises(InvalidMetadataFilterError):
            await store.search(metadata={"a.b": "x"}, user_id="alice")

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_metadata_updates_merge_and_can_leave_the_sort_key_alone(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        await store.create("thread-1", user_id="alice", metadata={"topic": "中文"})
        when = datetime(2026, 1, 1, tzinfo=UTC)
        await _stamp(store, "thread-1", when)
        await store.update_metadata(
            "thread-1",
            {"pin": True, "user_id": "hidden"},
            touch=False,
            user_id="alice",
        )
        pinned = await store.get("thread-1", user_id="alice")
        assert pinned is not None
        assert pinned["metadata"] == {"topic": "中文", "pin": True}
        assert pinned["updated_at"] == iso_utc(when)

        await store.update_metadata("thread-1", {"pin": False}, user_id="alice")
        touched = await store.get("thread-1", user_id="alice")
        assert touched is not None
        assert touched["metadata"]["pin"] is False
        assert touched["updated_at"] != iso_utc(when)

        await store.update_display_name("thread-1", "", user_id="alice")
        named = await store.get("thread-1", user_id="alice")
        assert named is not None and named["display_name"] is None
        await store.update_display_name("thread-1", "标题", user_id="bob")
        unchanged = await store.get("thread-1", user_id="alice")
        assert unchanged is not None and unchanged["display_name"] is None

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_delete_removes_only_the_projection_row(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        await store.create("thread-1", user_id="alice")
        await store.delete("thread-1", user_id="bob")
        assert await store.get("thread-1", user_id="alice") is not None
        await store.delete("thread-1", user_id="alice")
        assert await store.get("thread-1", user_id=None) is None

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_check_access_allows_a_missing_thread_until_it_must_exist(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        assert await store.check_access("missing", "alice", require_existing=False) is True
        assert await store.check_access("missing", "alice", require_existing=True) is False
        await store.create("thread-1", user_id="alice")
        assert await store.check_access("thread-1", "alice") is True
        assert await store.check_access("thread-1", "bob", require_existing=True) is False

    _run(scenario)
