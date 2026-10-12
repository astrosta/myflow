"""M9 §2.5 / §2.9 / §2.11 / §2.12：认证门默认拒绝。

对着 §9 的 `test_auth_gate.py` 写：OpenAPI 里非白名单默认 401；cookie 存在但 tv 不匹配 401；
JWT 不出现在响应体；PAT 打 change-password → 403 session_required。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.app.gateway.app import create_app
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.auth_middleware import PUBLIC_EXACT_PATHS
from fakes.auth_app import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    TEST_SECRET,
    USER_EMAIL,
    default_username,
    make_auth_app,
)

#: 每条路由都要有一个占位参数，否则 401 之前会先撞到 404（路径不匹配也算"没过门"，
#: 但那样测的就不是认证了）。
_PATH_PARAMS = {
    "thread_id": "thread-1",
    "run_id": "run-1",
    "filename": "a.txt",
    "name": "foo",
    "model_name": "m",
    "path": "notes/a.md",
}


def _concrete(path: str) -> str:
    for placeholder, value in _PATH_PARAMS.items():
        path = path.replace("{" + placeholder + "}", value)
    return path.replace("{path:path}", "notes/a.md")


def test_openapi_routes_outside_the_whitelist_are_all_gated() -> None:
    """**新加一条路由默认就是受保护的**——反过来的默认值只要忘一个装饰器就是一个洞。"""
    application = create_app()
    application.state.auth_settings = AuthSettings(
        enabled=True, jwt_secret=TEST_SECRET, enable_docs=False
    )
    client = TestClient(application, raise_server_exceptions=False)

    checked = 0
    for path, operations in application.openapi()["paths"].items():
        if path in PUBLIC_EXACT_PATHS or any(
            path.startswith(prefix) for prefix in ("/api/v1/auth/oauth/", "/api/v1/auth/callback/")
        ):
            continue
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            response = client.request(method.upper(), _concrete(path))
            assert response.status_code == 401, f"{method.upper()} {path}"
            assert response.json()["code"] == "unauthenticated"
            checked += 1
    assert checked > 20, "the inventory should cover the whole documented surface"


def test_public_whitelist_routes_answer_without_a_session() -> None:
    harness = make_auth_app()
    assert harness.client.get("/api/v1/auth/setup-status").status_code == 200
    assert harness.client.get("/api/v1/auth/providers").json() == {"local": True}
    # logout 没有会话也是 200（幂等），且不清别人
    assert harness.client.post("/api/v1/auth/logout").status_code == 200


def test_docs_leave_the_whitelist_when_disabled() -> None:
    open_docs = make_auth_app(enable_docs=True)
    closed_docs = make_auth_app(enable_docs=False)

    assert open_docs.client.get("/docs").status_code == 200
    denied = closed_docs.client.get("/docs")
    assert denied.status_code == 401
    assert denied.json()["code"] == "unauthenticated"


def test_a_cookie_that_does_not_verify_is_401_not_anonymous() -> None:
    harness = make_auth_app()
    harness.client.cookies.set("access_token", "not-a-jwt")
    response = harness.client.get("/api/probe")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_login_response_carries_no_token_string() -> None:
    harness = make_auth_app()
    asyncio.run(harness.seed_admin())

    response = harness.login(ADMIN_EMAIL, ADMIN_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"expires_in", "needs_setup"}
    assert "eyJ" not in response.text  # JWT 的固定前缀
    assert "authorization" not in {key.lower() for key in response.headers}
    assert harness.client.cookies.get("access_token")


def test_login_failures_name_the_specific_reason() -> None:
    """默认（`auth.local.specific_errors=true`）：用户点一次就知道下一步该做什么。"""
    harness = make_auth_app()
    asyncio.run(harness.seed_admin())

    unknown = harness.login("nobody@example.com", ADMIN_PASSWORD)
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "account_not_found"
    assert "nobody@example.com" in unknown.json()["detail"]

    wrong = harness.login(ADMIN_EMAIL, "wrong-password")
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "wrong_password"
    assert "wrong-password" not in wrong.text  # 口令永不回显


def test_the_enumeration_safe_mode_collapses_every_reason() -> None:
    """`specific_errors=false`：宁可让用户多点两次，也不让人探测邮箱是否注册。"""
    harness = make_auth_app(specific_errors=False)
    asyncio.run(harness.seed_admin())

    unknown = harness.login("nobody@example.com", ADMIN_PASSWORD)
    wrong = harness.login(ADMIN_EMAIL, "wrong-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"] == "invalid_credentials"
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_setup_status_and_initialize_are_one_shot() -> None:
    harness = make_auth_app()
    assert harness.client.get("/api/v1/auth/setup-status").json() == {"needs_setup": True}

    first = harness.initialize(ADMIN_EMAIL, ADMIN_PASSWORD)
    assert first.status_code == 201
    assert first.json()["system_role"] == "admin"
    assert first.json()["username"] == default_username(ADMIN_EMAIL)
    assert harness.client.get("/api/v1/auth/setup-status").json() == {"needs_setup": False}

    second = harness.initialize("other@example.com", ADMIN_PASSWORD)
    assert second.status_code == 409
    assert second.json()["code"] == "admin_already_exists"


def test_registration_follows_the_switch() -> None:
    closed = make_auth_app(allow_registration=False)
    denied = closed.register(USER_EMAIL, "user-password")
    assert denied.status_code == 403
    assert denied.json()["code"] == "registration_closed"

    opened = make_auth_app(allow_registration=True)
    created = opened.register(USER_EMAIL, "user-password", username="Alice")
    assert created.status_code == 201
    assert created.json()["system_role"] == "user"
    assert created.json()["username"] == "alice", "用户名写入时小写归一"


def test_a_weak_password_is_422_without_echoing_it() -> None:
    harness = make_auth_app(allow_registration=True)

    weak = "zqx"
    short = harness.register(USER_EMAIL, weak)
    assert short.status_code == 422
    assert short.json()["code"] == "password_too_short"
    assert "at least 8" in short.json()["detail"] and "got 3" in short.json()["detail"]
    assert weak not in short.text

    # 超过 bcrypt 的 72 字节上限：说清超了几个字节，同样不回显口令
    over_long = "p" * 100
    too_long = harness.register(USER_EMAIL, over_long)
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "password_too_long"
    assert "72 bytes" in too_long.json()["detail"] and "100 bytes" in too_long.json()["detail"]
    assert over_long not in too_long.text


def test_extra_fields_are_rejected() -> None:
    harness = make_auth_app()
    response = harness.client.post(
        "/api/v1/auth/login/local",
        json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "is_admin": True},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_auth_disabled_injects_one_fixed_user_and_keeps_the_default_bucket() -> None:
    """§2.11：`enabled=false` 是单人演示，不是"没有身份"。"""
    harness = make_auth_app(enabled=False)

    anonymous = harness.client.get("/api/probe")
    assert anonymous.status_code == 200
    assert anonymous.json()["user_id"] == "default"  # M4 时代的桶名，绝不能换
    # 业务路由同样免认证，且写进 default 桶（`caller.user_id` 就是 default）
    created = harness.client.post("/api/threads", json={"thread_id": "t-demo"})
    assert created.status_code == 201
    row = asyncio.run(harness.thread_store.get("t-demo", user_id=None))
    assert row is not None
    assert row["user_id"] == "default"


def test_needs_setup_only_allows_me_and_change_password() -> None:
    """§2.13 不变量 3：管理员重置密码之后，用户在拿到新密码之前不该能用旧会话做事。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    user = asyncio.run(harness.seed_user())

    new_password = asyncio.run(
        harness.service.admin_reset_password(actor_id=admin["id"], user_id=user["id"])
    )
    harness.clear_cookies()
    assert harness.login(USER_EMAIL, new_password).status_code == 200

    blocked = harness.client.get("/api/threads")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "setup_required"
    assert harness.client.get("/api/v1/auth/me").status_code == 200  # 前端要知道为什么被拦

    changed = harness.post(
        "/api/v1/auth/change-password",
        json={"current_password": new_password, "new_password": "brand-new-password"},
    )
    assert changed.status_code == 200
    harness.clear_cookies()
    assert harness.login(USER_EMAIL, "brand-new-password").status_code == 200
    assert harness.client.get("/api/threads").status_code == 200


def test_pat_cannot_reach_session_only_routes() -> None:
    """§2.12：改密与 /me 是**仅会话**的；PAT 打它们是 403 `session_required`。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=admin["id"], name="script", scopes=["runs:read"], days=1, settings=harness.settings
        )
    )
    headers = {"Authorization": f"Bearer {plain}"}

    assert harness.client.get("/api/probe", headers=headers).status_code == 200
    for response in (
        harness.client.get("/api/v1/auth/me", headers=headers),
        harness.client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={"current_password": ADMIN_PASSWORD, "new_password": "another-password"},
        ),
    ):
        assert response.status_code == 403
        assert response.json()["code"] == "session_required"


def test_internal_token_represents_an_existing_owner() -> None:
    """§2.9 的三关：令牌 → owner 存在且启用 → 通道绑定（9D 注册）。"""
    harness = make_auth_app(internal_tokens=("shared-secret",))
    owner = asyncio.run(harness.seed_user())
    from agentkit.utils import make_safe_user_id

    storage_id = make_safe_user_id(owner["id"])
    headers = {
        "X-DeerFlow-Internal-Token": "shared-secret",
        "X-DeerFlow-Owner-User-Id": storage_id,
    }

    allowed = harness.client.get("/api/probe", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json()["user_id"] == storage_id
    assert allowed.json()["is_internal"] is True

    bad_token = harness.client.get(
        "/api/probe", headers={**headers, "X-DeerFlow-Internal-Token": "nope"}
    )
    assert bad_token.status_code == 401
    assert bad_token.json()["code"] == "unauthenticated"

    unknown_owner = harness.client.get(
        "/api/probe", headers={**headers, "X-DeerFlow-Owner-User-Id": "ghost"}
    )
    assert unknown_owner.status_code == 401
    assert unknown_owner.json()["code"] == "unknown_owner"

    missing_owner = harness.client.get(
        "/api/probe", headers={"X-DeerFlow-Internal-Token": "shared-secret"}
    )
    assert missing_owner.status_code == 401
    assert missing_owner.json()["code"] == "unknown_owner"


def test_rotation_accepts_the_old_and_the_new_token() -> None:
    harness = make_auth_app(internal_tokens=("new-token", "old-token"))
    owner = asyncio.run(harness.seed_user())
    from agentkit.utils import make_safe_user_id

    for token in ("new-token", "old-token"):
        response = harness.client.get(
            "/api/probe",
            headers={
                "X-DeerFlow-Internal-Token": token,
                "X-DeerFlow-Owner-User-Id": make_safe_user_id(owner["id"]),
            },
        )
        assert response.status_code == 200, token


def test_a_disabled_owner_cannot_be_represented() -> None:
    harness = make_auth_app(internal_tokens=("shared-secret",))
    owner = asyncio.run(harness.seed_user())
    asyncio.run(harness.store.update(owner["id"], disabled=True))
    from agentkit.utils import make_safe_user_id

    response = harness.client.get(
        "/api/probe",
        headers={
            "X-DeerFlow-Internal-Token": "shared-secret",
            "X-DeerFlow-Owner-User-Id": make_safe_user_id(owner["id"]),
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_owner"


def test_a_stale_cookie_cannot_lock_the_auth_pages() -> None:
    """浏览器里剩一枚旧 cookie（换过 jwt_secret / 库重建过 / token_version 变了）时，
    **登录、注册、初始化这些公开端点必须照常工作**——否则用户被永久锁在登录页外面，
    而且看到的还是一句"登录状态已失效"（真实踩过的坑）。
    """
    harness = make_auth_app()
    asyncio.run(harness.seed_admin())
    stale_token = "stale.jwt.from.an.older-secret"
    browser = harness.replay(stale_token)

    # 公开路由：陈旧 cookie 降级成匿名，各自给出自己的错误
    assert browser.get("/api/v1/auth/setup-status").json() == {"needs_setup": False}
    assert browser.get("/api/v1/auth/providers").status_code == 200

    failed = browser.login("nobody@example.com", ADMIN_PASSWORD)
    assert failed.status_code == 401
    assert failed.json()["code"] == "account_not_found"

    # 受保护路由仍然是 401（这里不能降级）
    blocked = browser.get("/api/probe")
    assert blocked.status_code == 401
    assert blocked.json()["code"] == "unauthenticated"

    # 正确登录照常成功，响应里带来的是一枚**新的** cookie
    signed_in = browser.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    assert signed_in.status_code == 200
    issued = [v for v in signed_in.headers.get_list("set-cookie") if v.startswith("access_token=")]
    assert issued, "登录必须重新种 cookie"
    fresh_token = issued[0].split(";", 1)[0].split("=", 1)[1]
    assert fresh_token != stale_token
    assert harness.replay(fresh_token).get("/api/probe").status_code == 200


def test_error_body_keeps_the_trace_id() -> None:
    harness = make_auth_app()
    response = harness.client.get("/api/probe")

    body = response.json()
    assert set(body) == {"detail", "code", "trace_id"}
    assert json.loads(json.dumps(body))["trace_id"]
