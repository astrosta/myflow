"""认证错误必须具体：每个失败都有自己的 code，detail 要能照着做。

用户名是唯一登录标识：登录、注册、初始化都只接受 username + password。
"""

from __future__ import annotations

import asyncio

import pytest

from fakes.auth_app import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    USER_USERNAME,
    make_auth_app,
)

SECRET_LIKE = "correct-horse-battery"


def _app(**kwargs):
    harness = make_auth_app(**kwargs)
    asyncio.run(harness.seed_admin())
    return harness


def _body(response) -> dict:
    body = response.json()
    assert set(body) == {"detail", "code", "trace_id"}
    return body


# --- 注册 / 初始化 ---


def test_a_bad_username_says_which_rule_failed() -> None:
    harness = make_auth_app(allow_registration=True)

    cases = [
        ("ab", "3-32 characters"),
        ("a" * 33, "3-32 characters"),
        ("-leading", "start and end with a letter or digit"),
        ("trailing-", "start and end with a letter or digit"),
        ("with space", "must use letters, digits"),
        ("default", "reserved"),
    ]
    for username, fragment in cases:
        response = harness.register(username, SECRET_LIKE)
        body = _body(response)
        assert response.status_code == 422, username
        assert body["code"] == "invalid_username", username
        assert fragment in body["detail"], (username, body["detail"])


def test_an_email_shaped_username_is_not_accepted() -> None:
    harness = make_auth_app(allow_registration=True)

    response = harness.register("alice@example.com", SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "invalid_username"


def test_a_too_short_password_reports_the_actual_length() -> None:
    harness = make_auth_app(allow_registration=True)

    response = harness.register(USER_USERNAME, "abc")

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "password_too_short"
    assert "at least 8 characters" in body["detail"]
    assert "got 3" in body["detail"]


def test_a_too_long_password_counts_bytes_not_characters() -> None:
    harness = make_auth_app(allow_registration=True)

    cjk = "密码口令安全" * 5  # 30 字符 / 90 字节
    response = harness.register(USER_USERNAME, cjk)

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "password_too_long"
    assert "72 bytes" in body["detail"]
    assert "90 bytes" in body["detail"]
    assert cjk not in response.text


def test_registration_does_not_require_an_email() -> None:
    harness = make_auth_app(allow_registration=True)

    response = harness.register(USER_USERNAME, SECRET_LIKE)

    assert response.status_code == 201
    assert response.json()["username"] == USER_USERNAME
    assert response.json()["email"] is None


def test_an_email_field_is_rejected() -> None:
    harness = make_auth_app(allow_registration=True)

    response = harness.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "username": USER_USERNAME, "password": SECRET_LIKE},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_registration_closed_says_what_to_do() -> None:
    harness = _app(allow_registration=False)

    response = harness.register(USER_USERNAME, SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 403
    assert body["code"] == "registration_closed"
    assert "admin" in body["detail"]


def test_a_second_initialize_does_not_leak_the_admin_username() -> None:
    harness = _app()

    response = harness.initialize(USER_USERNAME, SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 409
    assert body["code"] == "admin_already_exists"
    assert USER_USERNAME not in body["detail"]


# --- 登录 ---


def test_an_unknown_username_says_so() -> None:
    harness = _app()

    response = harness.login("nobody", ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "account_not_found"
    assert "nobody" in body["detail"]
    assert "register" in body["detail"]


def test_a_malformed_username_at_login_is_not_treated_as_an_email() -> None:
    harness = _app()

    response = harness.login("alice@nowhere", ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "invalid_username"


def test_a_taken_username_names_the_field_to_change() -> None:
    harness = _app(allow_registration=True)

    taken = harness.register(ADMIN_USERNAME, SECRET_LIKE)
    body = _body(taken)
    assert taken.status_code == 409
    assert body["code"] == "username_taken"
    assert ADMIN_USERNAME in body["detail"]


def test_a_user_can_log_in_with_the_username() -> None:
    harness = _app()

    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200
    harness.clear_cookies()
    assert harness.login("ADMIN", ADMIN_PASSWORD).status_code == 200
    assert harness.client.get("/api/v1/auth/me").json()["username"] == ADMIN_USERNAME


def test_a_wrong_password_says_so_without_echoing_it() -> None:
    harness = _app()
    attempt = "definitely-not-the-password"

    response = harness.login(ADMIN_USERNAME, attempt)

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "wrong_password"
    assert attempt not in response.text
    assert ADMIN_PASSWORD not in response.text


def test_a_disabled_account_is_reported_only_after_the_password_checks_out() -> None:
    harness = _app()
    other = asyncio.run(harness.seed_user())
    admin = asyncio.run(harness.store.get_by_username(ADMIN_USERNAME))
    asyncio.run(
        harness.service.set_disabled(actor_id=admin["id"], user_id=other["id"], disabled=True)
    )

    wrong = harness.login(USER_USERNAME, "wrong-password")
    assert wrong.status_code == 401
    assert _body(wrong)["code"] == "wrong_password"

    right = harness.login(USER_USERNAME, USER_PASSWORD)
    body = _body(right)
    assert right.status_code == 403
    assert body["code"] == "account_disabled"
    assert USER_USERNAME in body["detail"]


def test_a_soft_deleted_account_says_it_is_pending_deletion() -> None:
    harness = _app()
    other = asyncio.run(harness.seed_user())
    admin = asyncio.run(harness.store.get_by_username(ADMIN_USERNAME))
    asyncio.run(harness.service.delete_user(actor_id=admin["id"], user_id=other["id"]))

    response = harness.login(USER_USERNAME, USER_PASSWORD)

    body = _body(response)
    assert response.status_code == 403
    assert body["code"] == "account_deleted"
    assert "restore" in body["detail"]


def test_rate_limiting_tells_the_client_how_long_to_wait() -> None:
    harness = make_auth_app(max_login_attempts=2, lockout_seconds=120)
    asyncio.run(harness.seed_admin())
    assert harness.login(ADMIN_USERNAME, "wrong-1").status_code == 401
    assert harness.login(ADMIN_USERNAME, "wrong-2").status_code == 401

    response = harness.login(ADMIN_USERNAME, ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 429
    assert body["code"] == "login_rate_limited"
    retry_after = int(response.headers["Retry-After"])
    assert 120 <= retry_after <= 121
    assert f"try again in {retry_after} seconds" in body["detail"]


# --- 改密 ---


def test_change_password_reports_the_current_one_first() -> None:
    harness = _app()
    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200

    response = harness.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong-current", "new_password": "abc"},
    )

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "invalid_credentials"
    assert "current password" in body["detail"]
    assert "wrong-current" not in response.text


def test_change_password_reports_the_new_password_policy() -> None:
    harness = _app()
    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200

    response = harness.post(
        "/api/v1/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "abc"},
    )

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "password_too_short"
    assert "got 3" in body["detail"]


# --- 收敛模式 ---


def test_the_enumeration_safe_mode_hides_every_reason() -> None:
    harness = make_auth_app(specific_errors=False)
    admin = asyncio.run(harness.seed_admin())
    other = asyncio.run(harness.seed_user())
    asyncio.run(harness.service.set_disabled(actor_id=admin["id"], user_id=other["id"], disabled=True))

    answers = [
        harness.login("nobody", ADMIN_PASSWORD),
        harness.login(USER_USERNAME, "wrong-password"),
        harness.login(USER_USERNAME, USER_PASSWORD),
    ]

    assert [response.status_code for response in answers] == [401, 401, 401]
    assert {_body(response)["code"] for response in answers} == {"invalid_credentials"}
    assert len({_body(response)["detail"] for response in answers}) == 1
