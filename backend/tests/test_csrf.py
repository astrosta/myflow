"""M9 §2.6：CSRF double-submit + Origin 检查。

对着 §9 的 `test_csrf.py` 写：POST 缺 header 拒；bootstrap 不要求 token 但拒 hostile Origin；
**坏 Bearer 不回退 cookie**。
"""

from __future__ import annotations

import asyncio

from fakes.auth_app import ACCESS_COOKIE_NAME, ADMIN_EMAIL, ADMIN_PASSWORD, CSRF_COOKIE_NAME, USER_EMAIL, make_auth_app


def _logged_in(**kwargs):
    harness = make_auth_app(**kwargs)
    asyncio.run(harness.seed_admin())
    assert harness.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
    return harness


def test_state_changing_without_the_header_is_rejected() -> None:
    harness = _logged_in()
    assert harness.csrf_token  # 登录响应成对发了 csrf cookie

    response = harness.client.post("/api/threads", json={"thread_id": "t1"})

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_the_matching_pair_passes() -> None:
    harness = _logged_in()

    response = harness.post("/api/threads", json={"thread_id": "t1"})

    assert response.status_code == 201


def test_reads_never_need_the_token() -> None:
    harness = _logged_in()
    assert harness.client.get("/api/threads").status_code == 200


def test_a_mismatched_pair_is_rejected() -> None:
    harness = _logged_in()

    response = harness.client.post(
        "/api/threads", json={"thread_id": "t1"}, headers={"X-CSRF-Token": "not-the-cookie"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_bootstrap_login_needs_no_token_but_rejects_a_hostile_origin() -> None:
    harness = make_auth_app()
    asyncio.run(harness.seed_admin())
    body = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}

    hostile = harness.client.post(
        "/api/v1/auth/login/local", json=body, headers={"Origin": "http://evil.example"}
    )
    assert hostile.status_code == 403
    assert hostile.json()["code"] == "origin_not_allowed"

    same_origin = harness.client.post(
        "/api/v1/auth/login/local", json=body, headers={"Origin": harness.base_url}
    )
    assert same_origin.status_code == 200


def test_a_configured_cross_origin_is_allowed_for_both_middlewares() -> None:
    """`GATEWAY_CORS_ORIGINS` 是**同一个**变量：CORS 与 CSRF 不能各有一套事实。"""
    harness = _logged_in(cors_origins=("http://app.example.com",))

    response = harness.post(
        "/api/threads", json={"thread_id": "t1"}, headers={"Origin": "http://app.example.com"}
    )

    assert response.status_code == 201


def test_hostile_origin_is_rejected_even_with_a_valid_token() -> None:
    harness = _logged_in()

    response = harness.post(
        "/api/threads", json={"thread_id": "t1"}, headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "origin_not_allowed"


def test_a_broken_bearer_never_falls_back_to_the_cookie() -> None:
    """攻击页面写一个坏 header 就能把请求降级成"带 cookie 的跨站请求"——这里堵住它。"""
    harness = _logged_in()

    response = harness.client.post(
        "/api/threads",
        json={"thread_id": "t1"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_pat_skips_the_cookie_pair_but_origin_still_counts() -> None:
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    plain, _row = asyncio.run(
        harness.service.create_pat(
            user_id=admin["id"],
            name="script",
            scopes=["threads:write"],
            days=1,
            settings=harness.settings,
        )
    )
    harness.clear_cookies()
    bearer = {"Authorization": f"Bearer {plain}"}

    created = harness.client.post("/api/threads", json={"thread_id": "t-pat"}, headers=bearer)
    assert created.status_code == 201

    hostile = harness.client.post(
        "/api/threads",
        json={"thread_id": "t-2"},
        headers={**bearer, "Origin": "http://evil.example"},
    )
    assert hostile.status_code == 403
    assert hostile.json()["code"] == "origin_not_allowed"


def test_logout_clears_both_cookies_and_never_reissues_csrf() -> None:
    harness = _logged_in()

    response = harness.client.post("/api/v1/auth/logout")

    assert response.status_code == 200
    issued = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{CSRF_COOKIE_NAME}=") and not value.startswith(f"{CSRF_COOKIE_NAME}=\"\"")
    ]
    assert issued == [], "logout must not stamp a fresh CSRF token"
    assert not harness.client.cookies.get(ACCESS_COOKIE_NAME)


def test_auth_disabled_has_nothing_to_protect() -> None:
    harness = make_auth_app(enabled=False)

    created = harness.client.post("/api/threads", json={"thread_id": "t-demo"})

    assert created.status_code == 201
