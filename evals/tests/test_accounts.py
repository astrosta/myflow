"""Fixed-account login, hashing, and cookie-isolation tests.

Everything here is black-box HTTP: a MockTransport stands in for the Gateway,
so the eval package never imports backend.app or agentkit.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from myflow_evals import accounts
from myflow_evals.accounts import (
    AccountCredential,
    hashed_langfuse_user_id,
    load_accounts,
    login_account,
    login_all,
    safe_agent_user_id,
)

SALT = "test-salt"
BASE_URL = "http://gateway.test/api"
ALICE_ID = "11111111-1111-4111-8111-111111111111"
BOB_ID = "22222222-2222-4222-8222-222222222222"
USER_IDS = {"alice": ALICE_ID, "bob": BOB_ID}
ALICE = AccountCredential(label="analyst_a", username="alice", password="alice-password")
BOB = AccountCredential(label="analyst_b", username="bob", password="bob-password")


def _cookie_value(header: str, name: str) -> str:
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    raise AssertionError(f"cookie {name!r} missing from {header!r}")


def _mock_transport(records: list[httpx.Request], *, needs_setup: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        records.append(request)
        if request.url.path == "/api/v1/auth/login/local":
            body = json.loads(request.content)
            username = body["username"]
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", f"access_token=access-{username}; Path=/; HttpOnly"),
                    ("set-cookie", f"csrf_token=csrf-{username}; Path=/"),
                ],
                json={"expires_in": 3600, "needs_setup": needs_setup},
            )
        if request.url.path == "/api/v1/auth/me":
            token = _cookie_value(request.headers["cookie"], "access_token")
            username = token.removeprefix("access-")
            assert request.headers["x-csrf-token"] == f"csrf-{username}"
            return httpx.Response(
                200,
                json={
                    "id": USER_IDS[username],
                    "email": f"{username}@example.com",
                    "username": username,
                    "system_role": "user",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    records: list[httpx.Request],
    *,
    needs_setup: bool = False,
) -> list[httpx.Client]:
    """Make every httpx.Client created by accounts use one MockTransport."""
    real_client = httpx.Client
    transport = _mock_transport(records, needs_setup=needs_setup)
    created: list[httpx.Client] = []

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(accounts.httpx, "Client", factory)
    return created


def test_load_accounts_reads_labeled_json_file(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "analyst_a": {"username": "alice", "password": "alice-password"},
                "analyst_b": {"username": "bob", "password": "bob-password"},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_accounts(path)

    assert loaded == [ALICE, BOB]
    assert "alice-password" not in repr(loaded[0])


def test_load_accounts_reads_agent_users_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AGENT_USERS", json.dumps({"analyst_a": {"username": "alice", "password": "alice-password"}})
    )

    assert load_accounts(None) == [ALICE]


def test_load_accounts_requires_credentials_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_USERS", raising=False)

    with pytest.raises(RuntimeError, match="AGENT_USERS"):
        load_accounts(None)


def test_safe_agent_user_id_keeps_safe_values_unchanged() -> None:
    assert safe_agent_user_id("user-123_ABC") == "user-123_ABC"


def test_safe_agent_user_id_matches_backend_fallback() -> None:
    assert safe_agent_user_id("alice@example.com") == "alice-example-com-ff8d9819fc0e12bf"


def test_safe_agent_user_id_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        safe_agent_user_id("")


def test_hashed_langfuse_user_id_is_hmac_sha256_prefix() -> None:
    assert hashed_langfuse_user_id("user-123", SALT) == "5cda4f8667a32a3a6cc0560dd4044cef"
    assert hashed_langfuse_user_id("alice-example-com-ff8d9819fc0e12bf", SALT) == (
        "240325b300de6d1b5ea55d55c1fa3c93"
    )


@pytest.mark.parametrize("base_url", ["http://gateway.test", "http://gateway.test/api"])
def test_login_account_uses_cookie_jar_csrf_and_me(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    records: list[httpx.Request] = []
    clients = _patch_client(monkeypatch, records)

    session = login_account(ALICE, base_url=base_url, salt=SALT)

    assert [request.url.path for request in records] == [
        "/api/v1/auth/login/local",
        "/api/v1/auth/me",
    ]
    assert json.loads(records[0].content) == {
        "username": "alice",
        "password": "alice-password",
    }
    assert len(clients) == 1
    assert session.client is clients[0]
    assert session.credential is ALICE
    assert session.user_id == ALICE_ID
    assert session.hashed_user_id == "72f503ec34733e6a5bf295169ae31a5c"
    assert session.client.cookies.get("access_token") == "access-alice"
    assert session.client.cookies.get("csrf_token") == "csrf-alice"
    assert session.client.headers["X-CSRF-Token"] == "csrf-alice"


def test_login_account_fails_on_needs_setup_without_calling_me(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[httpx.Request] = []
    clients = _patch_client(monkeypatch, records, needs_setup=True)

    with pytest.raises(RuntimeError, match="needs_setup") as excinfo:
        login_account(ALICE, base_url=BASE_URL, salt=SALT)

    assert [request.url.path for request in records] == ["/api/v1/auth/login/local"]
    assert "alice-password" not in str(excinfo.value)
    assert len(clients) == 1
    assert clients[0].is_closed


def test_login_all_keys_sessions_by_hash_and_isolates_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[httpx.Request] = []
    clients = _patch_client(monkeypatch, records)

    sessions = login_all([ALICE, BOB], base_url=BASE_URL, salt=SALT)

    assert set(sessions) == {
        "72f503ec34733e6a5bf295169ae31a5c",
        "17fa5a0ad54439108a6061e15f4ef632",
    }
    alice = sessions["72f503ec34733e6a5bf295169ae31a5c"]
    bob = sessions["17fa5a0ad54439108a6061e15f4ef632"]
    assert alice.user_id == ALICE_ID
    assert bob.user_id == BOB_ID
    assert len(clients) == 2
    assert [request.url.path for request in records].count("/api/v1/auth/login/local") == 2
    assert alice.client is not bob.client

    assert alice.client.cookies.get("access_token") == "access-alice"
    assert bob.client.cookies.get("access_token") == "access-bob"
    assert alice.client.cookies.get("access_token") not in {
        cookie for cookie in bob.client.cookies.values()
    }
    assert bob.client.cookies.get("access_token") not in {
        cookie for cookie in alice.client.cookies.values()
    }

    # Probe each jar after login: the handler rejects a mismatched CSRF token and
    # records exactly which access cookie travelled on each request.
    before = len(records)
    for session in (alice, bob):
        assert session.client.get("/v1/auth/me").status_code == 200
    probes = records[before:]
    assert len(probes) == 2
    assert _cookie_value(probes[0].headers["cookie"], "access_token") == "access-alice"
    assert _cookie_value(probes[1].headers["cookie"], "access_token") == "access-bob"
