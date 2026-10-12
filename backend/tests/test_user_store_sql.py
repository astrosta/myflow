"""`SqlUserStore`：真表、真会话。

内存后端掩盖不了的差异都在这里钉住——最典型的是
`session.scalar()` 收到 ScalarSelect 会直接抛 `ArgumentError`，
而内存实现同一段逻辑跑得好好的（只有真跑 SQL 才看得出来）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.persistence.base import Base
from agentkit.utils import make_safe_user_id
from backend.app.gateway.auth.models import UserRow  # noqa: F401 —— 让 Base.metadata 认得 users
from backend.app.gateway.auth.store.base import UserAlreadyExistsError
from backend.app.gateway.auth.store.sql import SqlUserStore


@pytest.fixture
def store(tmp_path: Path):
    async def _build():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "users.db").as_posix())
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    engine = asyncio.run(_build())
    yield SqlUserStore(async_sessionmaker(engine, expire_on_commit=False))
    asyncio.run(engine.dispose())


def test_a_user_round_trips(store: SqlUserStore) -> None:
    async def scenario() -> None:
        created = await store.create(user_id="u1", email="a@b.com", username="alice", password_hash="hash")
        assert created["username"] == "alice"
        assert created["id"] == "u1"
        assert created["system_role"] == "user"
        assert created["token_version"] == 0
        assert created["created_at"].endswith("Z")
        assert (await store.get("u1"))["email"] == "a@b.com"
        assert (await store.get_by_email("a@b.com"))["id"] == "u1"
        assert await store.get_by_email("missing@b.com") is None

    asyncio.run(scenario())


def test_get_by_storage_id_handles_both_shapes(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create(
            user_id="a-b-c14cddc033f64b9d", email="weird@b.com", username="weird", password_hash="h"
        )
        # 存储 ID 与主键同名：走主键快路径
        assert (await store.get_by_storage_id("a-b-c14cddc033f64b9d"))["email"] == "weird@b.com"
        # 主键不是 uuid 时，存储 ID 是派生的：回退扫一遍也要找得到
        await store.create(
            user_id="user@example.com", email="email-as-id@b.com", username="emailid", password_hash="h"
        )
        derived = make_safe_user_id("user@example.com")
        assert derived != "user@example.com"  # 确实不是主键本身
        found = await store.get_by_storage_id(derived)
        assert found is not None and found["id"] == "user@example.com"
        assert await store.get_by_storage_id("ghost") is None

    asyncio.run(scenario())


def test_the_first_admin_is_created_only_once(store: SqlUserStore) -> None:
    """条件插入：第二个并发 initialize 拿不到行，路由把它翻成 409。"""

    async def scenario() -> None:
        first = await store.create_first_admin(
            user_id="u1", email="a@b.com", username="alice", password_hash="h"
        )
        assert first is not None and first["system_role"] == "admin"

        second = await store.create_first_admin(
            user_id="u2", email="b@b.com", username="bob", password_hash="h"
        )
        assert second is None
        assert await store.active_admin_count() == 1
        assert await store.first_admin_id() == "u1"

    asyncio.run(scenario())


def test_admins_can_be_added_demoted_and_counted(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create_first_admin(
            user_id="u1", email="a@b.com", username="alice", password_hash="h"
        )
        await store.create(
            user_id="u2", email="b@b.com", username="bob", password_hash="h", system_role="admin"
        )
        assert await store.active_admin_count() == 2

        assert await store.conditional_demote("u2") is True
        assert await store.conditional_demote("u1") is False  # 最后一个 admin
        assert (await store.get("u1"))["system_role"] == "admin"
        assert (await store.get("u2"))["system_role"] == "user"

    asyncio.run(scenario())


def test_soft_delete_refuses_the_last_admin(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create_first_admin(
            user_id="u1", email="a@b.com", username="alice", password_hash="h"
        )
        await store.create(user_id="u2", email="b@b.com", username="bob", password_hash="h")

        assert await store.conditional_soft_delete("u1", deleted_at=datetime.now(UTC)) is False
        assert await store.conditional_soft_delete("u2", deleted_at=datetime.now(UTC)) is True
        assert (await store.get("u2"))["deleted_at"] is not None
        assert await store.conditional_soft_delete("u2", deleted_at=datetime.now(UTC)) is False

    asyncio.run(scenario())


def test_bump_token_version_is_monotonic(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create(user_id="u1", email="a@b.com", username="alice", password_hash="h")
        assert (await store.bump_token_version("u1"))["token_version"] == 1
        assert (await store.bump_token_version("u1"))["token_version"] == 2
        assert await store.bump_token_version("ghost") is None

    asyncio.run(scenario())


def test_update_validates_columns_and_email_uniqueness(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create(user_id="u1", email="a@b.com", username="alice", password_hash="h")
        await store.create(user_id="u2", email="b@b.com", username="bob", password_hash="h")

        with pytest.raises(ValueError):
            await store.update("u1", password="nope")
        with pytest.raises(UserAlreadyExistsError) as email_clash:
            await store.update("u1", email="b@b.com")
        assert email_clash.value.field == "email", "撞的是邮箱，用户要改的是邮箱"
        with pytest.raises(UserAlreadyExistsError) as username_clash:
            await store.update("u1", username="bob")
        assert username_clash.value.field == "username"
        assert (await store.get("u1"))["email"] == "a@b.com"
        assert await store.update("ghost", email="c@b.com") is None

    asyncio.run(scenario())


def test_lookup_by_username_and_identifier(store: SqlUserStore) -> None:
    """两个登录标识各自 unique，`get_by_identifier` 两条都能命中。"""

    async def scenario() -> None:
        await store.create(user_id="u1", email="a@b.com", username="alice", password_hash="h")

        assert (await store.get_by_username("alice"))["id"] == "u1"
        assert await store.get_by_username("nobody") is None
        assert (await store.get_by_identifier("a@b.com"))["id"] == "u1"
        assert (await store.get_by_identifier("alice"))["id"] == "u1"
        assert await store.get_by_identifier("ghost") is None

    asyncio.run(scenario())


def test_a_duplicate_username_is_reported_as_such(store: SqlUserStore) -> None:
    async def scenario() -> None:
        await store.create(user_id="u1", email="a@b.com", username="alice", password_hash="h")

        with pytest.raises(UserAlreadyExistsError) as raised:
            await store.create(user_id="u2", email="b@b.com", username="alice", password_hash="h")
        assert raised.value.field == "username"

    asyncio.run(scenario())


def test_pats_round_trip(store: SqlUserStore) -> None:
    async def scenario() -> None:
        expires = datetime.now(UTC) + timedelta(days=1)
        created = await store.create_pat(
            pat_id="p1",
            user_id="u1",
            name="script",
            token_hash="digest-1",
            token_prefix="dfp_abcd",
            token_version=3,
            scopes=["runs:read"],
            expires_at=expires,
        )
        assert created["token_version"] == 3
        assert (await store.get_pat_by_hash("digest-1"))["id"] == "p1"
        assert await store.get_pat_by_hash("nope") is None
        assert await store.count_pats("u1") == 1

        await store.touch_pat("p1", when=datetime.now(UTC))
        listed = await store.list_pats("u1")
        assert listed[0]["last_used_at"] is not None

        assert await store.delete_pat("p1", user_id="u2") is False
        assert await store.delete_pat("p1", user_id="u1") is True
        assert await store.list_pats("u1") == []

    asyncio.run(scenario())
