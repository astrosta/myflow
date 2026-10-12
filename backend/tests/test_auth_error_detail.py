"""认证错误必须**具体**：每个失败都有自己的 `code`，detail 要能照着做。

对照表（`auth.local.specific_errors=true`，默认）：

| 情形 | HTTP | code |
|---|---|---|
| 邮箱形状不对 | 422 | `invalid_email` |
| 口令短于 8 位 | 422 | `password_too_short` |
| 口令超过 72 字节 | 422 | `password_too_long` |
| 邮箱已注册 | 409 | `user_already_exists` |
| 自助注册关闭 | 403 | `registration_closed` |
| 已有 admin 再 initialize | 409 | `admin_already_exists` |
| 没这个账号（邮箱或用户名） | 401 | `account_not_found` |
| 用户名形状不对 / 保留字 | 422 | `invalid_username` |
| 用户名被占 | 409 | `username_taken` |
| 密码不对 | 401 | `wrong_password` |
| 账号被禁用（口令对） | 403 | `account_disabled` |
| 账号在回收期（口令对） | 403 | `account_deleted` |
| 失败次数超限 | 429 | `login_rate_limited`（+ `Retry-After`） |

两条不变量贯穿全部用例：**明文口令永不回显**；
**账号状态只在口令正确之后才透露**。
"""

from __future__ import annotations

import asyncio

import pytest

from fakes.auth_app import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_EMAIL,
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


@pytest.mark.parametrize(
    ("email", "fragment"),
    [
        ("", "must not be empty"),
        ("foo", "exactly one '@'"),
        ("foo@", "domain after '@'"),
        ("@bar.com", "part before '@'"),
        ("a b@c.com", "whitespace"),
        ("a@b", "domain with a dot"),
        ("a@@b.com", "exactly one '@'"),
    ],
)
def test_a_bad_email_says_which_rule_failed(email: str, fragment: str) -> None:
    harness = make_auth_app(allow_registration=True)

    # 用户名显式给一个合法的：这一组测的是**邮箱**的规则，
    # 别让派生出来的用户名先把 422 抢走（"" 和 "@bar.com" 的 local part 是空的）。
    response = harness.register(email, SECRET_LIKE, username="someone")

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "invalid_email"
    assert fragment in body["detail"]


def test_a_too_short_password_reports_the_actual_length() -> None:
    harness = make_auth_app(allow_registration=True)

    response = harness.register(USER_EMAIL, "abc")

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "password_too_short"
    assert "at least 8 characters" in body["detail"]
    assert "got 3" in body["detail"]


def test_a_too_long_password_counts_bytes_not_characters() -> None:
    """6 个汉字 = 18 字节，看起来短、其实按 bcrypt 的规矩是"没超"；
    40 个汉字 = 120 字节，必须报超。"""
    harness = make_auth_app(allow_registration=True)

    cjk = "密码口令安全" * 5  # 30 字符 / 90 字节
    response = harness.register(USER_EMAIL, cjk)

    body = _body(response)
    assert response.status_code == 422
    assert body["code"] == "password_too_long"
    assert "72 bytes" in body["detail"]
    assert "90 bytes" in body["detail"]
    assert cjk not in response.text


def test_a_duplicate_email_names_the_address() -> None:
    harness = _app(allow_registration=True)

    response = harness.register(ADMIN_EMAIL, SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 409
    assert body["code"] == "user_already_exists"
    assert ADMIN_EMAIL in body["detail"]
    assert "sign in instead" in body["detail"]


def test_registration_closed_says_what_to_do() -> None:
    harness = _app(allow_registration=False)

    response = harness.register(USER_EMAIL, SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 403
    assert body["code"] == "registration_closed"
    assert "admin" in body["detail"]


def test_a_second_initialize_does_not_leak_the_admin_address() -> None:
    harness = _app()

    response = harness.initialize(USER_EMAIL, SECRET_LIKE)

    body = _body(response)
    assert response.status_code == 409
    assert body["code"] == "admin_already_exists"
    assert ADMIN_EMAIL not in body["detail"], "公开路由不点名现有 admin"


# --- 登录 ---


def test_an_unknown_email_says_so() -> None:
    harness = _app()

    response = harness.login("nobody@example.com", ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "account_not_found"
    assert "nobody@example.com" in body["detail"]
    assert "register" in body["detail"]


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
        response = harness.register(USER_EMAIL, SECRET_LIKE, username=username)
        body = _body(response)
        assert response.status_code == 422, username
        assert body["code"] == "invalid_username", username
        assert fragment in body["detail"], (username, body["detail"])


def test_a_taken_username_names_the_field_to_change() -> None:
    """邮箱撞车和用户名撞车是两个 code：用户要改的字段不一样。"""
    harness = _app(allow_registration=True)

    taken = harness.register(USER_EMAIL, SECRET_LIKE, username="admin")
    body = _body(taken)
    assert taken.status_code == 409
    assert body["code"] == "username_taken"
    assert "admin" in body["detail"]

    duplicate_email = harness.register(ADMIN_EMAIL, SECRET_LIKE, username="fresh-name")
    assert duplicate_email.status_code == 409
    assert _body(duplicate_email)["code"] == "user_already_exists"


def test_a_user_can_log_in_with_the_username() -> None:
    """用户名是**第二个登录标识**，不是显示名。"""
    harness = _app()

    by_username = harness.login("admin", ADMIN_PASSWORD)
    assert by_username.status_code == 200

    by_email = harness.client.post(
        "/api/v1/auth/login/local", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert by_email.status_code == 200, "旧的 email 字段名仍然接受（AliasChoices）"

    harness.clear_cookies()
    assert harness.login("ADMIN", ADMIN_PASSWORD).status_code == 200, "大小写不敏感"
    assert harness.client.get("/api/v1/auth/me").json()["username"] == "admin"


def test_a_bad_username_at_login_blames_the_username() -> None:
    harness = _app()

    response = harness.login("no-such-user", ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "account_not_found"
    assert "no-such-user" in body["detail"]


def test_a_wrong_password_says_so_without_echoing_it() -> None:
    harness = _app()
    attempt = "definitely-not-the-password"

    response = harness.login(ADMIN_EMAIL, attempt)

    body = _body(response)
    assert response.status_code == 401
    assert body["code"] == "wrong_password"
    assert attempt not in response.text
    assert ADMIN_PASSWORD not in response.text


def test_a_malformed_email_at_login_blames_the_email_not_the_password() -> None:
    """带 @ 但当邮箱不成形 → 说邮箱；不带 @ → 按**用户名**查。"""
    harness = _app()

    malformed = harness.login("alice@nowhere", ADMIN_PASSWORD)
    body = _body(malformed)
    assert malformed.status_code == 422
    assert body["code"] == "invalid_email"
    assert "domain with a dot" in body["detail"]

    # 不含 @ 的串是**合法的用户名形状**，查不到就是查不到，不是"格式错"
    missing = harness.login("not-a-registered-name", ADMIN_PASSWORD)
    assert missing.status_code == 401
    assert _body(missing)["code"] == "account_not_found"


def test_a_login_identifier_without_at_is_looked_up_as_a_username() -> None:
    harness = _app()

    assert harness.login(ADMIN_USERNAME, ADMIN_PASSWORD).status_code == 200

    wrong = harness.login("someone-else", ADMIN_PASSWORD)
    assert wrong.status_code == 401
    assert _body(wrong)["code"] == "account_not_found"


def test_a_disabled_account_is_reported_only_after_the_password_checks_out() -> None:
    """不知道口令的人不该从响应里读出"这个账号存在且被禁用了"。"""
    harness = _app()
    other = asyncio.run(harness.seed_user())
    admin = asyncio.run(harness.store.get_by_email(ADMIN_EMAIL))
    asyncio.run(
        harness.service.set_disabled(actor_id=admin["id"], user_id=other["id"], disabled=True)
    )

    wrong = harness.login(USER_EMAIL, "wrong-password")
    assert wrong.status_code == 401
    assert _body(wrong)["code"] == "wrong_password"

    right = harness.login(USER_EMAIL, USER_PASSWORD)
    body = _body(right)
    assert right.status_code == 403
    assert body["code"] == "account_disabled"
    assert USER_USERNAME in body["detail"], "详情里用用户名，用户认得出那是自己"


def test_a_soft_deleted_account_says_it_is_pending_deletion() -> None:
    harness = _app()
    other = asyncio.run(harness.seed_user())
    admin = asyncio.run(harness.store.get_by_email(ADMIN_EMAIL))
    asyncio.run(harness.service.delete_user(actor_id=admin["id"], user_id=other["id"]))

    response = harness.login(USER_EMAIL, USER_PASSWORD)

    body = _body(response)
    assert response.status_code == 403
    assert body["code"] == "account_deleted"
    assert "restore" in body["detail"]


def test_rate_limiting_tells_the_client_how_long_to_wait() -> None:
    harness = make_auth_app(max_login_attempts=2, lockout_seconds=120)
    asyncio.run(harness.seed_admin())
    assert harness.login(ADMIN_EMAIL, "wrong-1").status_code == 401
    assert harness.login(ADMIN_EMAIL, "wrong-2").status_code == 401

    response = harness.login(ADMIN_EMAIL, ADMIN_PASSWORD)

    body = _body(response)
    assert response.status_code == 429
    assert body["code"] == "login_rate_limited"
    # 向上取整：还剩 119.99 秒时不能说"0 秒后重试"
    retry_after = int(response.headers["Retry-After"])
    assert 120 <= retry_after <= 121
    assert f"try again in {retry_after} seconds" in body["detail"], "头和 detail 必须说同一个数"


# --- 改密 ---


def test_change_password_reports_the_current_one_first() -> None:
    harness = _app()
    assert harness.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200

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
    assert harness.login(ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200

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
        harness.login("nobody@example.com", ADMIN_PASSWORD),
        harness.login(USER_EMAIL, "wrong-password"),
        harness.login(USER_EMAIL, USER_PASSWORD),  # 被禁用，但口令对
    ]

    assert [response.status_code for response in answers] == [401, 401, 401]
    assert {_body(response)["code"] for response in answers} == {"invalid_credentials"}
    assert len({_body(response)["detail"] for response in answers}) == 1
