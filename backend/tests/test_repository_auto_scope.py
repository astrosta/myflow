"""M9 §2.3：仓储层的三态 `user_id`。

`AUTO` 从 ContextVar 解析；没有用户 → **抛错，绝不退化成全局查询**。
`str` 显式指定；`None` 跳过隔离——只允许迁移脚本和管理员 CLI。
跨用户读到的行返回 `None`（路由把它映射成 404，不是 403）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.persistence.base import Base
from agentkit.persistence.thread_meta import make_thread_meta_store
from agentkit.persistence.thread_meta.base import ThreadMetaStore, ThreadOwnershipConflictError
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.thread_meta.sql import SqlThreadMetaStore
from agentkit.runtime.user_context import (
    AUTO,
    MissingUserContextError,
    reset_current_user,
    set_current_user,
)


@pytest.fixture(autouse=True)
def _reset_user_context():
    token = set_current_user(None)
    yield
    reset_current_user(token)


def _memory() -> ThreadMetaStore:
    return MemoryThreadMetaStore()


def _sql(tmp_path: Path) -> ThreadMetaStore:
    """真表、真会话：SQL 后端的 AUTO 语义必须和内存后端一致。"""

    async def _build():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "app.db").as_posix())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    return asyncio.run(_build())


@pytest.fixture(params=["memory", "sql"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        yield _memory()
        return
    engine, session_factory = _sql(tmp_path)
    store = make_thread_meta_store(session_factory)
    assert isinstance(store, SqlThreadMetaStore)
    try:
        yield store
    finally:
        asyncio.run(engine.dispose())


def test_auto_without_context_fails_closed_for_every_method(store) -> None:
    """退化成全局查询的后果是"单用户测试全绿、多用户串号"。"""
    calls = {
        "get": lambda: store.get("t1"),
        "search": lambda: store.search(),
        "delete": lambda: store.delete("t1"),
        "update_status": lambda: store.update_status("t1", "idle"),
    }
    for method, call in calls.items():
        with pytest.raises(MissingUserContextError) as raised:
            asyncio.run(call())
        assert f"ThreadMetaStore.{method}" in str(raised.value)


def test_create_refuses_a_null_user(store) -> None:
    with pytest.raises(MissingUserContextError):
        asyncio.run(store.create("t1", user_id=None))


def test_auto_reads_the_current_user(store) -> None:
    token = set_current_user("alice")
    try:
        asyncio.run(store.create("t1"))
        row = asyncio.run(store.get("t1"))
    finally:
        reset_current_user(token)

    assert row is not None and row["user_id"] == "alice"


def test_a_cross_user_get_returns_none_not_a_row(store) -> None:
    token = set_current_user("alice")
    try:
        asyncio.run(store.create("t1"))
    finally:
        reset_current_user(token)

    token = set_current_user("bob")
    try:
        assert asyncio.run(store.get("t1")) is None
        assert asyncio.run(store.search()) == []
        assert asyncio.run(store.check_access("t1", "bob")) is False
    finally:
        reset_current_user(token)


def test_explicit_user_id_ignores_the_context(store) -> None:
    token = set_current_user("alice")
    try:
        asyncio.run(store.create("t1"))
        row = asyncio.run(store.get("t1", user_id="bob"))
    finally:
        reset_current_user(token)

    assert row is None


def test_explicit_none_is_the_global_query(store) -> None:
    """只给迁移脚本和 CLI 用：调用点必须**看得见**这个 `user_id=None`。"""
    token = set_current_user("alice")
    try:
        asyncio.run(store.create("t1"))
    finally:
        reset_current_user(token)

    row = asyncio.run(store.get("t1", user_id=None))

    assert row is not None and row["user_id"] == "alice"


def test_another_user_cannot_take_over_a_thread_id(store) -> None:
    """`threads_meta.thread_id` 是全局主键：撞车是 409 `thread_ownership_conflict`，
    **不是**静默接管（M4 §3.3 的约定，M9 的隔离建立在它上面）。"""
    token = set_current_user("alice")
    try:
        asyncio.run(store.create("t1", display_name="alice's"))
    finally:
        reset_current_user(token)

    token = set_current_user("bob")
    try:
        with pytest.raises(ThreadOwnershipConflictError):
            asyncio.run(store.create("t1", display_name="bob's"))
    finally:
        reset_current_user(token)

    token = set_current_user("alice")
    try:
        row = asyncio.run(store.get("t1"))
    finally:
        reset_current_user(token)
    assert row is not None and row["display_name"] == "alice's"
