"""M9 §2.3 / §2.16：跨用户读到的一律 **404**（不是 403）。

403 会泄露"这个 thread id 存在"，而 thread id 是可枚举的。
admin 不是数据超级用户：他拿到同样的 404，只是日志里多一条审计。
"""

from __future__ import annotations

import asyncio
import logging

from fakes.auth_app import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    USER_EMAIL,
    USER_PASSWORD,
    make_auth_app,
    run_row,
)
from agentkit.utils import make_safe_user_id


def _two_clients(*, second_role: str = "user"):
    """两个用户、两个 cookie jar；写请求各自带自己的 CSRF 头。"""
    harness = make_auth_app()
    admin = asyncio.run(harness.seed_admin())
    second = asyncio.run(harness.seed_user(role=second_role))

    owner_client = harness.login_as(ADMIN_EMAIL, ADMIN_PASSWORD)
    other_client = harness.login_as(USER_EMAIL, USER_PASSWORD)
    return harness, admin, second, owner_client, other_client


def test_another_users_thread_is_404_not_403() -> None:
    harness, _admin, _second, owner_client, other_client = _two_clients()
    created = owner_client.post("/api/threads", json={"thread_id": "t-owner"})
    assert created.status_code == 201

    assert owner_client.get("/api/threads/t-owner").status_code == 200

    hidden = other_client.get("/api/threads/t-owner")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "thread_not_found"


def test_the_thread_list_only_shows_your_own() -> None:
    _harness, _admin, _second, owner_client, other_client = _two_clients()
    assert owner_client.post("/api/threads", json={"thread_id": "t-owner"}).status_code == 201

    page = other_client.get("/api/threads").json()

    assert page["data"] == []


def test_another_users_run_is_404() -> None:
    harness, admin, _second, owner_client, other_client = _two_clients()
    assert owner_client.post("/api/threads", json={"thread_id": "t-owner"}).status_code == 201
    storage_id = make_safe_user_id(admin["id"])
    harness.run_store._rows["run-1"] = run_row(run_id="run-1", thread_id="t-owner", user_id=storage_id)

    visible = owner_client.get("/api/threads/t-owner/runs/run-1")
    assert visible.status_code == 200

    hidden = other_client.get("/api/threads/t-owner/runs/run-1")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "run_not_found"

    # 别人的 thread 连 run 列表都看不到（thread 级的 404）
    assert other_client.get("/api/threads/t-owner/runs").status_code == 404


def test_messages_of_an_invisible_thread_are_404() -> None:
    _harness, _admin, _second, owner_client, other_client = _two_clients()
    assert owner_client.post("/api/threads", json={"thread_id": "t-owner"}).status_code == 201

    response = other_client.get("/api/threads/t-owner/messages")

    assert response.status_code == 404
    assert response.json()["code"] == "thread_not_found"


def test_uploads_of_an_invisible_thread_are_404() -> None:
    _harness, _admin, _second, owner_client, other_client = _two_clients()
    assert owner_client.post("/api/threads", json={"thread_id": "t-owner"}).status_code == 201

    response = other_client.get("/api/threads/t-owner/uploads/list")

    assert response.status_code == 404


def test_an_admin_gets_the_same_404_but_it_is_audited(caplog) -> None:
    """§2.16：越权尝试记日志，命中与否都一样。"""
    _harness, _admin, _second, _owner_client, other_client = _two_clients(second_role="admin")
    assert other_client.post("/api/threads", json={"thread_id": "t-of-the-other"}).status_code == 201

    with caplog.at_level(logging.WARNING, logger="backend.app.gateway.audit"):
        response = other_client.get("/api/threads/t-of-the-other")  # 自己的，不记
        assert response.status_code == 200
        hidden = other_client.get("/api/threads/t-somebody-else")
    assert hidden.status_code == 404
    assert any("admin_cross_user_access_denied" in record.getMessage() for record in caplog.records)
