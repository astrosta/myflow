"""M9 §2.7：`reset_admin` 离线救援。

三条纪律：随机口令、`token_version += 1`（旧 cookie 立刻失效）、**stdout 只打路径不打明文**。
没有 admin 时不在这里自动建号——那是 `/setup` 的活。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agentkit.config.app_config import get_app_config
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.runtime.user_context import set_current_user, reset_current_user
from backend.app.gateway.auth.password import verify_password
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.settings import AuthSettings
from backend.app.gateway.auth.store import make_user_store
from backend.app.gateway.auth import reset_admin
from fakes.skill_tree import write_app_config

SECRET = "step-up-secret-0123456789abcdef0123456789"
OLD_PASSWORD = "old-admin-password"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_app_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["database"] = {"backend": "sqlite", "sqlite_dir": str(tmp_path / "data")}
    data["auth"] = {"enabled": True, "jwt_secret": SECRET}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(path))
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    yield path
    asyncio.run(close_engine())


async def _seed_admin(username: str) -> dict:
    store = make_user_store(get_session_factory())
    service = AuthService(store)
    app_config = get_app_config()
    user, _token, _expires = await service.initialize(
        username=username,
        password=OLD_PASSWORD,
        settings=AuthSettings.from_config(app_config),
    )
    return user


def test_reset_admin_writes_a_credential_file_and_prints_only_the_path(
    config: Path, capsys, tmp_path: Path
) -> None:
    seed = asyncio.run(_boot_and_seed("admin"))
    credentials = tmp_path / "creds.txt"

    code = asyncio.run(reset_admin.reset_admin(username=None, credentials_file=credentials))

    assert code == 0
    out = capsys.readouterr().out
    assert str(credentials) in out
    assert OLD_PASSWORD not in out

    body = credentials.read_text(encoding="utf-8")
    new_password = body.split("password: ", 1)[1].splitlines()[0]
    assert new_password and new_password not in out
    assert "needs_setup" in body

    # 旧口令失效、新口令可用、token_version 已 +1、needs_setup=true
    async def _check() -> None:
        await init_engine_from_config(get_app_config().database)  # reset_admin 关掉了引擎
        try:
            store = make_user_store(get_session_factory())
            row = await store.get(seed["id"])
            assert row["token_version"] == seed["token_version"] + 1
            assert row["needs_setup"] is True
            assert not verify_password(OLD_PASSWORD, row["password_hash"])
            assert verify_password(new_password, row["password_hash"])
        finally:
            await close_engine()

    asyncio.run(_check())


def test_reset_admin_refuses_when_there_is_no_admin(config: Path, capsys, tmp_path: Path) -> None:
    asyncio.run(_init_and_close())

    code = asyncio.run(reset_admin.reset_admin(username=None, credentials_file=tmp_path / "creds.txt"))

    assert code == 3
    assert "no admin account found" in capsys.readouterr().err


def test_reset_admin_refuses_when_auth_is_off(tmp_path: Path, monkeypatch, capsys) -> None:
    path = write_app_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["auth"] = {"enabled": False}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(path))

    code = asyncio.run(reset_admin.reset_admin(username=None, credentials_file=tmp_path / "creds.txt"))

    assert code == 2
    assert "auth.enabled=false" in capsys.readouterr().err


async def _init_and_close() -> None:
    """建表：`init_engine_from_config` 会跑 bootstrap（空库 = create_all + stamp）。"""
    await init_engine_from_config(get_app_config().database)
    await close_engine()


async def _boot_and_seed(username: str) -> dict:
    """种一个 admin，然后把引擎关掉——`reset_admin` 自己会再开一次。"""
    await init_engine_from_config(get_app_config().database)
    try:
        return await _seed_admin(username)
    finally:
        await close_engine()
