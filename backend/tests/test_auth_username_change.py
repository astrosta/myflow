"""M9 §2.12：`PATCH /api/v1/auth/me` —— 改**自己**的用户名。

这是**协议**测试：前端设置面板（`settings-dialog.tsx` → `updateUsername()`）已经在调它，
形状对不上就是线上 404/422。所以这里逐条钉住：方法 + 路径、请求体、响应体、
状态码、以及三条"改名不该发生的事"（令牌不动、桶不动、邮箱不动）。
"""

from __future__ import annotations

import asyncio

import pytest

from agentkit.utils import make_safe_user_id
from fakes.auth_app import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_EMAIL,
    USER_PASSWORD,
    USER_USERNAME,
    make_auth_app,
)

PATH = "/api/v1/auth/me"


def _signed_in(**kwargs):
    """一个已登录的 harness + 它的管理员身份。"""
    harness = make_auth_app(**kwargs)
    admin = asyncio.run(harness.seed_admin())
    assert harness.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
    return harness, admin


def _body(response) -> dict:
    body = response.json()
    assert set(body) == {"detail", "code", "trace_id"}
    return body


# --- 协议形状 ---


def test_patch_me_renames_the_caller_and_returns_the_user() -> None:
    harness, admin = _signed_in()

    response = harness.patch(PATH, json={"username": "renamed-admin"})

    assert response.status_code == 200
    assert response.json() == {
        "id": admin["id"],
        "email": ADMIN_EMAIL,
        "username": "renamed-admin",
        "system_role": "admin",
    }
    assert asyncio.run(harness.store.get(admin["id"]))["username"] == "renamed-admin"


def test_the_new_username_becomes_a_login_identifier() -> None:
    harness, _admin = _signed_in()
    assert harness.patch(PATH, json={"username": "renamed"}).status_code == 200

    harness.clear_cookies()
    assert harness.login("renamed", ADMIN_PASSWORD).status_code == 200

    harness.clear_cookies()
    stale = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert stale.status_code == 401
    assert _body(stale)["code"] == "account_not_found"


def test_uppercase_is_normalized_not_rejected() -> None:
    harness, _admin = _signed_in()

    response = harness.patch(PATH, json={"username": "  ReNamed.User  "})

    assert response.status_code == 200
    assert response.json()["username"] == "renamed.user"


def test_no_token_string_in_the_response() -> None:
    harness, _admin = _signed_in()

    response = harness.patch(PATH, json={"username": "renamed"})

    assert "eyJ" not in response.text
    assert "authorization" not in {key.lower() for key in response.headers}


@pytest.mark.parametrize(
    ("username", "fragment"),
    [
        ("ab", "3-32 characters"),
        ("a" * 33, "3-32 characters"),
        ("-leading", "start and end with a letter or digit"),
        ("with space", "must use letters, digits"),
        ("default", "reserved"),
    ],
)
def test_a_bad_username_is_422_with_the_rule(username: str, fragment: str) -> None:
    harness, _admin = _signed_in()

    response = harness.patch(PATH, json={"username": username})

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "invalid_username"
    assert fragment in body["detail"]


def test_a_taken_username_is_409() -> None:
    harness, _admin = _signed_in()
    asyncio.run(harness.seed_user())

    response = harness.patch(PATH, json={"username": USER_USERNAME})

    body = _body(response)
    assert response.status_code == 409
    assert body["code"] == "username_taken"
    assert USER_USERNAME in body["detail"]
    assert asyncio.run(harness.store.get_by_username(USER_USERNAME))["email"] == USER_EMAIL


def test_patching_the_same_username_is_a_no_op() -> None:
    """重名是"被别人占了"（409），不变是"我自己"——两者不能混。"""
    harness, _admin = _signed_in()

    response = harness.patch(PATH, json={"username": ADMIN_USERNAME})

    assert response.status_code == 200
    assert response.json()["username"] == ADMIN_USERNAME


def test_extra_fields_are_rejected() -> None:
    """邮箱、角色、密码各有各的路径——一个 PATCH 不该顺手把别的也改了。"""
    harness, _admin = _signed_in()

    response = harness.patch(PATH, json={"username": "renamed", "email": "new@example.com"})

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "invalid_request"
    # pydantic 的 extra="forbid" 会点名那个多余字段——比一句"请求不合法"有用得多
    assert body["errors"][0]["loc"][-1] == "email"


# --- 改名**不该**发生的事 ---


def test_tokens_and_sessions_survive_a_rename() -> None:
    """改用户名不是撤销事件（§2.13 的失效矩阵）：`token_version` 不动。"""
    harness, admin = _signed_in()
    token = harness.client.cookies.get("access_token")
    before = asyncio.run(harness.store.get(admin["id"]))["token_version"]

    assert harness.patch(PATH, json={"username": "renamed"}).status_code == 200

    after = asyncio.run(harness.store.get(admin["id"]))["token_version"]
    assert after == before, "改名不该顺手把所有人的会话踢掉"
    assert harness.replay(token).get("/api/probe").status_code == 200
    assert harness.client.get("/api/probe").status_code == 200


def test_the_bucket_does_not_move() -> None:
    """§2.2 那颗雷：桶名派生自 `users.id`，不是用户名。"""
    from agentkit.runtime.user_context import storage_user_id

    harness, admin = _signed_in()
    before = make_safe_user_id(admin["id"])

    harness.patch(PATH, json={"username": "renamed"})
    row = asyncio.run(harness.store.get(admin["id"]))

    assert storage_user_id(row, raw_identity=row["username"]) == before


def test_the_email_is_untouched() -> None:
    harness, admin = _signed_in()

    harness.patch(PATH, json={"username": "renamed"})

    row = asyncio.run(harness.store.get(admin["id"]))
    assert row["email"] == ADMIN_EMAIL
    assert asyncio.run(harness.store.get_by_email(ADMIN_EMAIL))["id"] == admin["id"]


# --- 谁能打 ---


def test_a_pat_cannot_rename_the_account() -> None:
    """仅会话（§2.12）：一枚泄漏的长期令牌不该能改账号标识。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=admin["id"], name="script", scopes=["runs:read"], days=1, settings=harness.settings
        )
    )
    harness.clear_cookies()

    response = harness.client.patch(
        PATH, json={"username": "renamed"}, headers={"Authorization": f"Bearer {plain}"}
    )

    body = _body(response)
    assert response.status_code == 403
    assert body["code"] == "session_required"
    assert asyncio.run(harness.store.get(admin["id"]))["username"] == ADMIN_USERNAME


def test_anonymous_cannot_rename() -> None:
    harness = make_auth_app()
    asyncio.run(harness.seed_admin())

    response = harness.client.patch(PATH, json={"username": "renamed"})

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "unauthenticated"


def test_setup_required_must_change_the_password_first() -> None:
    """§2.13 不变量 3：管理员刚重置过密码的人，正事是改密码（而且 PATCH 也被拦）。

    GET `/me` 仍然放行——否则前端连"我是谁、为什么被拦"都问不出来。
    """
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    user = asyncio.run(harness.seed_user())
    new_password = asyncio.run(
        harness.service.admin_reset_password(actor_id=admin["id"], user_id=user["id"])
    )
    harness.clear_cookies()
    assert harness.login(USER_EMAIL, new_password).status_code == 200

    blocked = harness.patch(PATH, json={"username": "renamed"})
    body = _body(blocked)
    assert blocked.status_code == 403
    assert body["code"] == "setup_required"
    assert harness.client.get(PATH).status_code == 200
    assert asyncio.run(harness.store.get(user["id"]))["username"] == USER_USERNAME
