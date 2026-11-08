"""Fernet encryption for `mcp_servers.headers_encrypted`.

The ciphertext is the only representation of an MCP server's HTTP headers that may
reach the database, a log line, an API response or an exception.  Fernet gives us
authenticated encryption with a per-message IV, so a token is never stable and a
token written under another key can never be read back.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from agentkit.mcp.crypto import (
    MAX_HEADER_VALUE_CHARS,
    HeaderCipher,
)

HEADERS = {"Authorization": "Bearer secret", "X-Client": "mcp/1.0.0"}


def test_round_trip_and_owner_key() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())
    token = cipher.encrypt({"Authorization": "Bearer secret", "X-Client": "mcp/1.0.0"})
    assert token and "secret" not in token
    assert cipher.decrypt(token) == {"Authorization": "Bearer secret", "X-Client": "mcp/1.0.0"}
    with pytest.raises(ValueError):
        HeaderCipher(HeaderCipher.generate_key()).decrypt(token)


def test_no_headers_is_stored_as_null() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())

    assert cipher.encrypt(None) is None
    assert cipher.encrypt({}) is None
    assert cipher.decrypt(None) == {}
    assert cipher.decrypt("") == {}


def test_each_token_is_fresh_but_decrypts_to_the_same_headers() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())

    first = cipher.encrypt(HEADERS)
    second = cipher.encrypt(HEADERS)

    assert first != second
    assert cipher.decrypt(first) == HEADERS
    assert cipher.decrypt(second) == HEADERS


def test_key_may_come_from_the_config_as_a_secret_str() -> None:
    key = HeaderCipher.generate_key()

    token = HeaderCipher(SecretStr(key)).encrypt(HEADERS)

    assert HeaderCipher(key).decrypt(token) == HEADERS


def test_generate_key_produces_usable_distinct_keys() -> None:
    first = HeaderCipher.generate_key()
    second = HeaderCipher.generate_key()

    assert first != second
    assert HeaderCipher(first).decrypt(HeaderCipher(first).encrypt(HEADERS)) == HEADERS


def test_malformed_key_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="Fernet") as caught:
        HeaderCipher("not-a-key")

    assert "not-a-key" not in str(caught.value)


def test_decrypt_reports_a_value_error_without_leaking_the_token() -> None:
    token = HeaderCipher(HeaderCipher.generate_key()).encrypt(HEADERS)

    with pytest.raises(ValueError) as caught:
        HeaderCipher(HeaderCipher.generate_key()).decrypt(token)

    assert token not in str(caught.value)


def test_decrypt_rejects_a_payload_that_is_not_a_header_object() -> None:
    key = HeaderCipher.generate_key()
    payload = Fernet(key.encode("ascii")).encrypt(b'["Authorization"]')

    with pytest.raises(ValueError, match="headers"):
        HeaderCipher(key).decrypt(payload.decode("ascii"))


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer a\r\nX-Injected: 1"},
        {"Authorization": "Bearer a\n"},
        {"Authorization": 42},
        {"": "value"},
        {"Auth orization": "value"},
        {"Authorization:": "value"},
    ],
)
def test_unsafe_headers_are_rejected(headers: dict[str, object]) -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())

    with pytest.raises(ValueError):
        cipher.encrypt(headers)  # type: ignore[arg-type]


def test_oversized_header_value_is_rejected() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())

    with pytest.raises(ValueError, match=str(MAX_HEADER_VALUE_CHARS)):
        cipher.encrypt({"Authorization": "x" * (MAX_HEADER_VALUE_CHARS + 1)})


def test_too_many_headers_are_rejected() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())
    headers = {f"X-Custom-{index}": "value" for index in range(64)}

    with pytest.raises(ValueError, match="headers"):
        cipher.encrypt(headers)
