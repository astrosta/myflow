"""Fixed test-account login and Langfuse-safe user identifiers.

This module is deliberately a black-box HTTP client: it imports neither
backend.app nor agentkit. Credentials live in AGENT_USERS or a secret file and
are only ever sent to the Gateway login route. Passwords are not logged and are
excluded from dataclass reprs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_SAFE_USER_ID_RE = re.compile(r"[^A-Za-z0-9_\-]")
_LOGIN_PATH = "/auth/login/local"
_ME_PATH = "/auth/me"
_DEFAULT_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class AccountCredential:
    """One fixed evaluation account. The password is intentionally absent from repr."""

    label: str
    username: str
    password: str = field(repr=False)


@dataclass
class AccountSession:
    """A logged-in account and the single Client that owns its cookie jar."""

    credential: AccountCredential
    client: httpx.Client
    user_id: str
    hashed_user_id: str


def load_accounts(
    path: Path | None, *, environ: Mapping[str, str] | None = None
) -> list[AccountCredential]:
    """Load fixed accounts from a secret JSON file or AGENT_USERS.

    The accepted shape is a mapping from label to an object with non-empty
    username and password strings::

        {"analyst_a": {"username": "alice", "password": "..."}}

    AGENT_USERS uses the same JSON mapping. ``environ`` defaults to ``os.environ``
    so the CLI can hand over the environment it merged from secret files. Error
    messages identify the source and labels but never echo passwords.
    """
    env = os.environ if environ is None else environ
    if path is None:
        raw = env.get("AGENT_USERS")
        if not raw or not raw.strip():
            raise RuntimeError(
                "AGENT_USERS is not set; provide fixed-account credentials there "
                "or pass a secret credentials file"
            )
        source = "AGENT_USERS"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid account credentials in {source}: {exc.msg}") from None
    else:
        credentials_path = Path(path)
        source = str(credentials_path)
        text = credentials_path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid account credentials in {source}: {exc.msg}") from None

    if not isinstance(data, Mapping):
        raise ValueError(
            f"invalid account credentials in {source}: expected a mapping of "
            "label to {username, password}"
        )

    accounts: list[AccountCredential] = []
    for label, entry in data.items():
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"invalid account credentials in {source}: labels must be non-empty strings"
            )
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"invalid account credentials in {source}: account {label!r} "
                "must be an object with username and password"
            )
        username = entry.get("username")
        password = entry.get("password")
        if not isinstance(username, str) or not username:
            raise ValueError(
                f"invalid account credentials in {source}: account {label!r} "
                "has no non-empty username"
            )
        if not isinstance(password, str) or not password:
            raise ValueError(
                f"invalid account credentials in {source}: account {label!r} "
                "has no non-empty password"
            )
        accounts.append(AccountCredential(label=label, username=username, password=password))
    return accounts


def safe_agent_user_id(raw: str) -> str:
    """Mirror agentkit.utils.make_safe_user_id for the storage-safe user ID.

    Safe values pass through unchanged. Unsafe characters become hyphens and a
    16-hex sha256 suffix preserves collision resistance.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("user_id must be a non-empty string.")
    sanitized = _SAFE_USER_ID_RE.sub("-", raw)
    if raw == sanitized:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized}-{digest}"


def hashed_langfuse_user_id(agent_user_id: str, salt: str) -> str:
    """Return Langfuse's hashed user ID: HMAC-SHA256(salt, agent_user_id)[:32]."""
    digest = hmac.new(salt.encode("utf-8"), agent_user_id.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def login_account(
    credential: AccountCredential, *, base_url: str, salt: str
) -> AccountSession:
    """Log in once and return the account's session (caller must close the client).

    The Gateway stores the access token in an HttpOnly cookie, so the Client's
    cookie jar is the only source of authentication. A fresh Client is created
    per account to prevent cookie leakage between users.
    """
    base = base_url.rstrip("/")
    if not base:
        raise ValueError("base_url must not be empty")
    client = httpx.Client(base_url=base, timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        login_response = client.post(
            _auth_path(base, _LOGIN_PATH),
            json={"username": credential.username, "password": credential.password},
        )
        login_response.raise_for_status()
        login_body = _json_object(login_response, f"login for account {credential.label!r}")

        needs_setup = login_body.get("needs_setup")
        if not isinstance(needs_setup, bool):
            raise RuntimeError(
                f"login for account {credential.label!r} did not return boolean needs_setup"
            )
        if needs_setup:
            raise RuntimeError(
                f"test account {credential.label!r} still requires password setup "
                "(needs_setup=true); change its password before running evaluations"
            )

        csrf_token = client.cookies.get("csrf_token")
        if not csrf_token:
            raise RuntimeError(
                f"login for account {credential.label!r} did not set a csrf_token cookie"
            )
        client.headers["X-CSRF-Token"] = csrf_token

        me_response = client.get(_auth_path(base, _ME_PATH))
        me_response.raise_for_status()
        me = _json_object(me_response, f"/me for account {credential.label!r}")
        user_id = me.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError(f"/me for account {credential.label!r} did not return a user id")

        return AccountSession(
            credential=credential,
            client=client,
            user_id=user_id,
            hashed_user_id=hashed_langfuse_user_id(safe_agent_user_id(user_id), salt),
        )
    except BaseException:
        client.close()
        raise


def login_all(
    accounts: Sequence[AccountCredential], *, base_url: str, salt: str
) -> dict[str, AccountSession]:
    """Log in each account once and key the sessions by Langfuse hashed user ID."""
    sessions: dict[str, AccountSession] = {}
    try:
        for credential in accounts:
            session = login_account(credential, base_url=base_url, salt=salt)
            if session.hashed_user_id in sessions:
                session.client.close()
                raise ValueError(
                    "two test accounts produced the same Langfuse hashed user id; "
                    "refusing to overwrite a session"
                )
            sessions[session.hashed_user_id] = session
        return sessions
    except BaseException:
        for session in sessions.values():
            session.client.close()
        raise


def _auth_path(base_url: str, suffix: str) -> str:
    """AGENT_API may include /api (the documented form); accept either spelling."""
    prefix = "/v1" if base_url.endswith("/api") else "/api/v1"
    return f"{prefix}{suffix}"


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"{what} returned a non-JSON response") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"{what} returned a non-object JSON response")
    return body
