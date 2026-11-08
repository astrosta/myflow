"""Fernet encryption for `mcp_servers.headers_encrypted`.

A user's MCP server headers usually carry a bearer token.  The ciphertext is the
only representation that may reach the database, a log line, an API response or an
exception message:

* Fernet gives authenticated encryption with a per-message timestamp and IV, so a
  token is never stable across writes (a stolen token cannot be matched against a
  later write) and a token written under a different key fails closed instead of
  decrypting to garbage;
* `encrypt` refuses headers that would let a caller inject a second header through
  a value (CRLF) or smuggle unbounded data into a column;
* `decrypt` turns every authentication/format failure into one `ValueError` that
  never contains the token, the key or the plaintext.

The key is the deployment's `mcp_servers.encryption_key`, never the auth/JWT secret:
rotating that secret must not make every stored header unreadable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

#: Enough for auth + tracing headers; small enough that a row cannot be a payload.
MAX_HEADERS = 32
MAX_HEADER_NAME_CHARS = 128
MAX_HEADER_VALUE_CHARS = 4096
#: The Task 3 API and the cipher share one budget: 32 KiB of header values.
MAX_HEADERS_TOTAL_BYTES = 32 * 1024
#: Backwards-compatible alias for callers that used the old character-named limit.
MAX_HEADERS_TOTAL_CHARS = MAX_HEADERS_TOTAL_BYTES

#: RFC 9110 field-name tokens.  No colon, no whitespace, no CR/LF.
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^`|~0-9A-Za-z]+$")


class HeaderCipher:
    """Encrypts and decrypts one deployment's MCP server headers."""

    def __init__(self, encryption_key: str | bytes | SecretStr) -> None:
        # Fernet validates the key itself; its message names the shape, never the key.
        self._fernet = Fernet(_key_bytes(encryption_key))

    @staticmethod
    def generate_key() -> str:
        """A fresh Fernet key as text, for `MYFLOW_MCP_SERVER_KEY`."""
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, headers: Mapping[str, str] | None) -> str | None:
        """The stored token, or `None` when there is nothing to store.

        `None`/empty means "this server needs no headers".  Storing an encrypted
        empty object instead would make "no auth" indistinguishable from "auth we
        failed to load".
        """
        checked = validate_headers(headers)
        if not checked:
            return None
        serialized = json.dumps(
            checked, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return self._fernet.encrypt(serialized.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> dict[str, str]:
        """The header map, or `{}` when the column is empty.

        A token that does not authenticate under this key raises `ValueError`: a
        caller must never treat undecryptable headers as "send no auth" silently.
        """
        if not token:
            return {}
        try:
            raw = self._fernet.decrypt(str(token).encode("utf-8"))
        except (InvalidToken, TypeError):
            raise ValueError(
                "mcp_servers.headers_encrypted could not be decrypted with the "
                "configured encryption_key"
            ) from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError(
                "mcp_servers.headers_encrypted does not hold a headers object"
            ) from None
        if not isinstance(parsed, dict):
            raise ValueError("mcp_servers.headers_encrypted must hold a headers object")
        return {str(name): str(value) for name, value in parsed.items()}


def _key_bytes(encryption_key: str | bytes | SecretStr) -> bytes:
    if isinstance(encryption_key, SecretStr):
        raw = encryption_key.get_secret_value()
    elif isinstance(encryption_key, bytes):
        raw = encryption_key.decode("ascii", "strict")
    else:
        raw = str(encryption_key or "")
    key = raw.strip()
    if not key:
        raise ValueError(
            "mcp_servers.encryption_key is empty: headers cannot be encrypted or "
            "decrypted without it"
        )
    return key.encode("utf-8")


def validate_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Validate a header map at the single storage boundary.

    Both the service and HeaderCipher.encrypt call this function, so the API and
    the stored token cannot drift on names, CRLF, entry count or the 32 KiB total.
    """

    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise ValueError("MCP server headers must be a mapping of header name to value")
    if len(headers) > MAX_HEADERS:
        raise ValueError(f"MCP server headers must hold at most {MAX_HEADERS} entries")
    checked: dict[str, str] = {}
    total_bytes = 0
    seen: set[str] = set()
    for raw_name, raw_value in headers.items():
        name = str(raw_name or "").strip()
        if (
            not name
            or len(name) > MAX_HEADER_NAME_CHARS
            or not _HEADER_NAME.fullmatch(name)
        ):
            raise ValueError(f"MCP server header name {raw_name!r} is not a valid HTTP header name")
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(
                f"MCP server header {name!r} is repeated with different casing"
            )
        seen.add(lowered)
        if not isinstance(raw_value, str):
            raise ValueError(f"MCP server header {name!r} must have a string value")
        if _has_control_chars(raw_value):
            raise ValueError(
                f"MCP server header {name!r} must not contain CR, LF or other control characters"
            )
        if len(raw_value) > MAX_HEADER_VALUE_CHARS:
            raise ValueError(
                f"MCP server header {name!r} must be at most {MAX_HEADER_VALUE_CHARS} characters"
            )
        checked[name] = raw_value
        total_bytes += len(raw_value.encode("utf-8"))
    if total_bytes > MAX_HEADERS_TOTAL_BYTES:
        raise ValueError(
            "MCP server headers must be at most "
            f"{MAX_HEADERS_TOTAL_BYTES} bytes in total"
        )
    return checked


def _has_control_chars(value: str) -> bool:
    """Header values are a single line: tab is allowed, everything else control is not."""
    return any(ord(char) < 0x20 and char != "\t" for char in value) or "\x7f" in value


__all__ = [
    "HeaderCipher",
    "MAX_HEADER_NAME_CHARS",
    "MAX_HEADERS",
    "MAX_HEADERS_TOTAL_BYTES",
    "MAX_HEADERS_TOTAL_CHARS",
    "MAX_HEADER_VALUE_CHARS",
    "validate_headers",
]
