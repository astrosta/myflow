"""M9 §2.13 / §2.15：账号生命周期与 PAT 的业务规则。

§2.13 的每个动作都要回答两个问题：**令牌还有效吗？数据还看得见吗？**
四条不变量（都属"最后一步才发现"的那类 bug）：至少一个 admin、
不能禁用/删除自己、needs_setup 只能改密、删除是软删 + 回收期。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentkit.config.auth_config import AuthConfig, LocalLoginConfig
from agentkit.config.paths import Paths
from backend.app.gateway.auth import buckets
from backend.app.gateway.auth.errors import InvalidScope, LastAdmin, TooManyPats
from backend.app.gateway.auth.models import PAT_SCOPE_MENU
from fakes.auth_app import (
    ACCESS_COOKIE_NAME,
    ADMIN_EMAIL,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    USER_EMAIL,
    USER_PASSWORD,
    make_auth_app,
)


def _two_people(role: str = "user", **kwargs):
    harness = make_auth_app(**kwargs)
    admin = asyncio.run(harness.seed_admin())
    user = asyncio.run(harness.seed_user(role=role))
    return harness, admin, user


# --- §2.13 四条不变量 ---


def test_the_last_admin_cannot_be_demoted_disabled_or_deleted() -> None:
    harness, admin, _user = _two_people()

    with pytest.raises(LastAdmin):
        asyncio.run(harness.service.set_role(actor_id=admin["id"], user_id=admin["id"], role="user"))
    with pytest.raises(LastAdmin):
        asyncio.run(harness.service.set_disabled(actor_id="someone", user_id=admin["id"], disabled=True))
    with pytest.raises(LastAdmin):
        asyncio.run(harness.service.delete_user(actor_id="someone", user_id=admin["id"]))

    row = asyncio.run(harness.store.get(admin["id"]))
    assert row["system_role"] == "admin"
    assert row["disabled"] is False
    assert row["deleted_at"] is None


def test_two_admins_may_demote_each_other() -> None:
    """条件写（`WHERE (SELECT count(*) FROM users WHERE admin) > 1`）而不是先查再写。"""
    harness, admin, _user = _two_people(role="admin")

    asyncio.run(harness.service.set_role(actor_id="someone", user_id=admin["id"], role="user"))

    row = asyncio.run(harness.store.get(admin["id"]))
    assert row["system_role"] == "user"


def test_you_cannot_disable_or_delete_yourself() -> None:
    harness, admin, _user = _two_people(role="admin")

    with pytest.raises(Exception) as disabled:
        asyncio.run(harness.service.set_disabled(actor_id=admin["id"], user_id=admin["id"], disabled=True))
    assert getattr(disabled.value, "code", None) == "cannot_modify_self"

    with pytest.raises(Exception) as deleted:
        asyncio.run(harness.service.delete_user(actor_id=admin["id"], user_id=admin["id"]))
    assert getattr(deleted.value, "code", None) == "cannot_modify_self"


def test_soft_delete_marks_the_bucket_and_restore_clears_it(tmp_path, monkeypatch) -> None:
    """§2.13 不变量 4：行保留、桶**先标记后删**，回收期内 admin 能恢复。"""
    monkeypatch.setattr(buckets, "get_paths", lambda: Paths(tmp_path))
    harness, admin, user = _two_people()
    from agentkit.utils import make_safe_user_id

    storage_id = make_safe_user_id(user["id"])
    bucket = tmp_path / "users" / storage_id
    bucket.mkdir(parents=True)

    asyncio.run(harness.service.delete_user(actor_id=admin["id"], user_id=user["id"]))
    assert buckets.is_user_bucket_marked_deleted(storage_id)
    assert bucket.exists(), "回收期内不能真删数据"

    asyncio.run(harness.service.restore_user(actor_id=admin["id"], user_id=user["id"]))
    assert not buckets.is_user_bucket_marked_deleted(storage_id)


def test_changing_the_email_never_moves_the_bucket() -> None:
    """§2.2 最容易埋的那颗雷：桶名派生自 identity 时，改邮箱 = 数据"消失"。"""
    from agentkit.runtime.user_context import storage_user_id

    harness, admin, user = _two_people()
    before = storage_user_id(user, raw_identity=USER_EMAIL)

    updated = asyncio.run(
        harness.service.set_email(actor_id=admin["id"], user_id=user["id"], email="renamed@example.com")
    )

    assert updated["email"] == "renamed@example.com"
    assert updated["id"] == user["id"]
    assert storage_user_id(updated, raw_identity=updated["email"]) == before


def test_changing_the_username_never_moves_the_bucket() -> None:
    """用户名看起来"本来就是 id"，比邮箱更容易被顺手拿去拼路径——所以它也要有这条测试。"""
    from agentkit.runtime.user_context import storage_user_id

    harness, admin, user = _two_people()
    before = storage_user_id(user, raw_identity=USER_EMAIL)

    updated = asyncio.run(
        harness.service.set_username(actor_id=admin["id"], user_id=user["id"], username="Renamed.User")
    )

    assert updated["username"] == "renamed.user", "写入时小写归一"
    assert updated["id"] == user["id"]
    assert storage_user_id(updated, raw_identity=updated["username"]) == before


def test_a_duplicate_username_is_rejected() -> None:
    harness, admin, user = _two_people()

    with pytest.raises(Exception) as raised:
        asyncio.run(
            harness.service.set_username(actor_id=admin["id"], user_id=user["id"], username=admin["username"])
        )
    assert getattr(raised.value, "code", None) == "username_taken"


def test_a_reserved_username_is_rejected() -> None:
    harness, admin, user = _two_people()

    with pytest.raises(Exception) as raised:
        asyncio.run(
            harness.service.set_username(actor_id=admin["id"], user_id=user["id"], username="default")
        )
    assert getattr(raised.value, "code", None) == "invalid_username"


def test_a_duplicate_email_is_rejected() -> None:
    harness, admin, user = _two_people()

    with pytest.raises(Exception) as raised:
        asyncio.run(
            harness.service.set_email(actor_id=admin["id"], user_id=user["id"], email=ADMIN_EMAIL)
        )
    assert getattr(raised.value, "code", None) == "user_already_exists"


# --- §2.5 登录限速 ---


def test_minimum_two_attempts_is_enforced_by_config() -> None:
    with pytest.raises(ValidationError):
        LocalLoginConfig(max_login_attempts=1)
    assert LocalLoginConfig(max_login_attempts=2).max_login_attempts == 2


def test_login_limit_is_counted_per_peer_ip() -> None:
    harness = make_auth_app(max_login_attempts=2, lockout_seconds=300)
    asyncio.run(harness.seed_admin())

    assert harness.login(ADMIN_USERNAME, "wrong-1").status_code == 401
    assert harness.login(ADMIN_USERNAME, "wrong-2").status_code == 401

    locked = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD)  # 口令对也不行
    assert locked.status_code == 429
    assert locked.json()["code"] == "login_rate_limited"


def test_x_real_ip_is_only_trusted_from_a_configured_proxy(monkeypatch) -> None:
    """`X-Forwarded-For` 永远不看：它能被客户端整段伪造。"""
    monkeypatch.setenv("AUTH_TRUSTED_PROXIES", "testclient")
    harness = make_auth_app(max_login_attempts=2, lockout_seconds=300)
    asyncio.run(harness.seed_admin())

    for _ in range(2):
        harness.client.post(
            "/api/v1/auth/login/local",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
            headers={"X-Real-IP": "10.0.0.9"},
        )

    other = harness.client.post(
        "/api/v1/auth/login/local",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        headers={"X-Real-IP": "10.0.0.10"},
    )
    assert other.status_code == 200

    same = harness.client.post(
        "/api/v1/auth/login/local",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        headers={"X-Real-IP": "10.0.0.9"},
    )
    assert same.status_code == 429


def test_an_untrusted_peer_cannot_forge_x_real_ip(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_TRUSTED_PROXIES", raising=False)
    harness = make_auth_app(max_login_attempts=2, lockout_seconds=300)
    asyncio.run(harness.seed_admin())

    for address in ("10.0.0.1", "10.0.0.2"):
        harness.client.post(
            "/api/v1/auth/login/local",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
            headers={"X-Real-IP": address},
        )

    # 换了 X-Real-IP 也还是同一个 peer：计数照样满
    assert (
        harness.client.post(
            "/api/v1/auth/login/local",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            headers={"X-Real-IP": "10.0.0.3"},
        ).status_code
        == 429
    )


def test_a_successful_login_clears_the_counter() -> None:
    harness = make_auth_app(max_login_attempts=3, lockout_seconds=300)
    asyncio.run(harness.seed_admin())

    assert harness.login(ADMIN_USERNAME, "wrong").status_code == 401
    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200
    assert harness.login(ADMIN_USERNAME, "wrong").status_code == 401
    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200


# --- §2.15 PAT 的业务规则 ---


def _pat(harness, user_id: str, **kwargs):
    payload = {"user_id": user_id, "name": "script", "scopes": ["runs:read"], "days": 1, "settings": harness.settings}
    payload.update(kwargs)
    return asyncio.run(harness.service.create_pat(**payload))


def test_pat_scopes_come_from_a_fixed_menu_and_never_include_admin() -> None:
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())

    with pytest.raises(InvalidScope):
        _pat(harness, admin["id"], scopes=["admin"])
    with pytest.raises(InvalidScope):
        _pat(harness, admin["id"], scopes=["runs:destroy"])
    with pytest.raises(InvalidScope):
        _pat(harness, admin["id"], scopes=[])

    assert "admin" not in PAT_SCOPE_MENU


def test_the_plaintext_is_shown_once_and_only_a_digest_is_stored() -> None:
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())

    plain, row = _pat(harness, admin["id"], scopes=["threads:read"])

    assert plain.startswith("dfp_")
    assert row["token_hash"] == hashlib.sha256(plain.encode()).hexdigest()
    assert row["token_prefix"] == plain[:8]
    assert plain not in row.values()
    listed = asyncio.run(harness.service.list_pats(admin["id"]))
    assert listed[0]["display"] == plain[:8] + "\u2026"
    assert plain not in str(listed)


def test_a_pat_lives_for_its_configured_window_only() -> None:
    harness = make_auth_app(pat_max_days=7)
    admin = asyncio.run(harness.seed_admin())

    with pytest.raises(ValueError):
        _pat(harness, admin["id"], days=8)
    with pytest.raises(ValueError):
        _pat(harness, admin["id"], days=0)

    plain, row = _pat(harness, admin["id"], days=1)
    bearer = {"Authorization": f"Bearer {plain}"}
    assert harness.client.get("/api/probe", headers=bearer).status_code == 200

    # 把到期时间挪到过去：到期即 401，不给宽限
    harness.store._pats[row["id"]]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert harness.client.get("/api/probe", headers=bearer).status_code == 401


def test_a_user_may_only_revoke_their_own_pat() -> None:
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    user = asyncio.run(harness.seed_user())
    plain, row = _pat(harness, user["id"])
    bearer = {"Authorization": f"Bearer {plain}"}

    assert asyncio.run(harness.service.revoke_pat(user_id=admin["id"], pat_id=row["id"])) is False
    assert harness.client.get("/api/probe", headers=bearer).status_code == 200

    assert asyncio.run(harness.service.revoke_pat(user_id=user["id"], pat_id=row["id"])) is True
    assert harness.client.get("/api/probe", headers=bearer).status_code == 401


def test_the_number_of_pats_is_capped() -> None:
    harness = make_auth_app(max_pats=1)
    admin = asyncio.run(harness.seed_admin())
    _pat(harness, admin["id"])

    with pytest.raises(TooManyPats):
        _pat(harness, admin["id"], name="second")


def test_last_used_at_is_throttled_to_a_minute() -> None:
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, row = _pat(harness, admin["id"])
    bearer = {"Authorization": f"Bearer {plain}"}

    assert harness.client.get("/api/probe", headers=bearer).status_code == 200
    first = harness.store._pats[row["id"]]["last_used_at"]
    assert first is not None

    assert harness.client.get("/api/probe", headers=bearer).status_code == 200
    assert harness.store._pats[row["id"]]["last_used_at"] == first, "一分钟内不重复写库"


def test_admin_capability_is_off_for_pats() -> None:
    """§2.15：PAT 走到管理类路由一律 403——即使它的主人是 admin。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, _row = _pat(harness, admin["id"], scopes=["skills:read"])
    bearer = {"Authorization": f"Bearer {plain}"}

    denied = harness.client.get("/api/mcp/config", headers=bearer)

    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
