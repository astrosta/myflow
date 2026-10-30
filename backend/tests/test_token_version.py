"""M9 §2.14：令牌失效矩阵。

"改密码之后旧 token 还能用吗"必须有唯一答案。M9 只有两个撤销机制：
`token_version`（服务端计数）与 `disabled` / `deleted_at`（服务端状态）。
**登出不是全局登出**——这一点连测试一起钉住。
"""

from __future__ import annotations

import asyncio

from fakes.auth_app import (
    ACCESS_COOKIE_NAME,
    ADMIN_EMAIL,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    USER_EMAIL,
    USER_USERNAME,
    USER_PASSWORD,
    make_auth_app,
)


def _logged_in(**kwargs):
    harness = make_auth_app(**kwargs)
    asyncio.run(harness.seed_admin())
    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200
    return harness


def _with_user(role: str = "user", **kwargs):
    harness = make_auth_app(**kwargs)
    admin = asyncio.run(harness.seed_admin())
    user = asyncio.run(harness.seed_user(role=role))
    return harness, admin, user


def test_change_password_kills_every_session() -> None:
    harness = _logged_in()
    old_token = harness.client.cookies.get(ACCESS_COOKIE_NAME)

    changed = harness.post(
        "/api/v1/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "new-password-1"},
    )

    assert changed.status_code == 200
    assert not harness.client.cookies.get(ACCESS_COOKIE_NAME), "改密后自己这枚也要失效"
    assert harness.replay(old_token).get("/api/probe").status_code == 401

    assert harness.login(ADMIN_USERNAME, "new-password-1").status_code == 200
    assert harness.client.get("/api/probe").status_code == 200


def test_change_password_needs_the_current_one() -> None:
    harness = _logged_in()

    response = harness.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong", "new_password": "new-password-1"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_change_password_also_kills_the_pats() -> None:
    """PAT 与 session 共享 `token_version`——这是刻意的（§2.14）。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=admin["id"], name="script", scopes=["runs:read"], days=1, settings=harness.settings
        )
    )
    bearer = {"Authorization": f"Bearer {plain}"}
    assert harness.client.get("/api/probe", headers=bearer).status_code == 200

    asyncio.run(
        harness.service.change_password(
            user_id=admin["id"], current_password=ADMIN_PASSWORD, new_password="new-password-1"
        )
    )

    assert harness.client.get("/api/probe", headers=bearer).status_code == 401


def test_logout_is_not_a_global_logout() -> None:
    """登出只清当前浏览器。写进用户文档的已知边界，也写进测试。"""
    harness = _logged_in()
    token = harness.client.cookies.get(ACCESS_COOKIE_NAME)

    assert harness.client.post("/api/v1/auth/logout").status_code == 200
    assert not harness.client.cookies.get(ACCESS_COOKIE_NAME)

    assert harness.replay(token).get("/api/probe").status_code == 200


def test_admin_reset_invalidates_sessions_and_pats() -> None:
    harness, admin, user = _with_user()
    assert harness.login(USER_USERNAME, USER_PASSWORD).status_code == 200
    token = harness.client.cookies.get(ACCESS_COOKIE_NAME)
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=user["id"], name="script", scopes=["runs:read"], days=1, settings=harness.settings
        )
    )

    new_password = asyncio.run(
        harness.service.admin_reset_password(actor_id=admin["id"], user_id=user["id"])
    )

    assert harness.replay(token).get("/api/probe").status_code == 401
    assert harness.client.get("/api/probe", headers={"Authorization": f"Bearer {plain}"}).status_code == 401
    refreshed = asyncio.run(harness.store.get(user["id"]))
    assert refreshed["needs_setup"] is True
    assert refreshed["password_hash"] != user["password_hash"]
    assert new_password != USER_PASSWORD


def test_disabling_an_account_invalidates_everything() -> None:
    harness, admin, user = _with_user()
    assert harness.login(USER_USERNAME, USER_PASSWORD).status_code == 200
    token = harness.client.cookies.get(ACCESS_COOKIE_NAME)
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=user["id"], name="script", scopes=["runs:read"], days=1, settings=harness.settings
        )
    )

    asyncio.run(harness.service.set_disabled(actor_id=admin["id"], user_id=user["id"], disabled=True))

    assert harness.replay(token).get("/api/probe").status_code == 401
    assert harness.client.get("/api/probe", headers={"Authorization": f"Bearer {plain}"}).status_code == 401
    # 口令是对的，所以这里可以说清是"被禁用"（§2.12 的具体错误）
    denied = harness.login(USER_USERNAME, USER_PASSWORD)
    assert denied.status_code == 403
    assert denied.json()["code"] == "account_disabled"


def test_a_soft_deleted_account_is_locked_out_until_restored() -> None:
    harness, admin, user = _with_user()
    assert harness.login(USER_USERNAME, USER_PASSWORD).status_code == 200
    token = harness.client.cookies.get(ACCESS_COOKIE_NAME)

    asyncio.run(harness.service.delete_user(actor_id=admin["id"], user_id=user["id"]))

    row = asyncio.run(harness.store.get(user["id"]))
    assert row is not None and row["deleted_at"] is not None  # 软删：行还在
    assert harness.replay(token).get("/api/probe").status_code == 401

    asyncio.run(harness.service.restore_user(actor_id=admin["id"], user_id=user["id"]))
    assert harness.login(USER_USERNAME, USER_PASSWORD).status_code == 200


def test_role_and_email_changes_keep_sessions_alive() -> None:
    """`sub` 是 uuid：改邮箱不该踢人，更不该换桶（§2.2）。"""
    harness, admin, user = _with_user()
    assert harness.login(USER_USERNAME, USER_PASSWORD).status_code == 200
    token = harness.client.cookies.get(ACCESS_COOKIE_NAME)

    asyncio.run(
        harness.service.set_email(actor_id=admin["id"], user_id=user["id"], email="renamed@example.com")
    )
    asyncio.run(harness.service.set_role(actor_id=admin["id"], user_id=user["id"], role="admin"))

    assert harness.replay(token).get("/api/probe").status_code == 200


def test_remember_me_only_changes_max_age() -> None:
    harness = _logged_in(base_url="http://localhost")  # 本机直连 HTTP：允许持久化
    harness.clear_cookies()

    response = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD, remember_me=True)
    cookies = "\n".join(response.headers.get_list("set-cookie"))

    assert "Max-Age=2592000" in cookies
    assert "HttpOnly" in cookies
    assert "Secure" not in cookies, "纯 HTTP 上打 Secure 等于把 cookie 丢掉"
    assert ADMIN_PASSWORD not in cookies


def test_a_public_http_origin_degrades_to_a_session_cookie() -> None:
    """公网 HTTP 沙箱 URL：即便 remember_me=true 也不持久化（§2.4）。"""
    harness = _logged_in(base_url="http://gateway.example.com")
    harness.clear_cookies()

    response = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD, remember_me=True)

    assert "Max-Age" not in "\n".join(response.headers.get_list("set-cookie"))


def test_https_marks_the_cookie_secure() -> None:
    harness = _logged_in(base_url="https://gateway.example.com")

    response = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD)

    assert "Secure" in "\n".join(response.headers.get_list("set-cookie"))
