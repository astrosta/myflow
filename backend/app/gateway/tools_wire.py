"""Tool wire masking and restoration for /api/tools.

The browser never sees raw ContextForge header values. A client that sends a
masked value back gets the stored value restored server-side; a masked value
without a stored counterpart is a validation error, never a literal credential.
"""

from __future__ import annotations

from collections.abc import Mapping

MASK = "****"

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-access-token",
    }
)


class InvalidToolHeader(ValueError):
    """A masked header cannot be restored or a header value is unsafe."""


def is_sensitive_header(name: str) -> bool:
    return name.strip().lower() in _SENSITIVE_HEADER_NAMES


def mask_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    masked: dict[str, str] = {}
    for key, value in (headers or {}).items():
        masked[str(key)] = MASK if is_sensitive_header(str(key)) and value else str(value)
    return masked


def restore_masked_headers(
    incoming: Mapping[str, str] | None,
    stored: Mapping[str, str] | None,
) -> dict[str, str]:
    restored: dict[str, str] = {}
    stored_map = {str(key): str(value) for key, value in (stored or {}).items()}
    for key, value in (incoming or {}).items():
        key_text = str(key)
        if value == MASK and is_sensitive_header(key_text):
            if key_text not in stored_map:
                raise InvalidToolHeader(
                    f"header {key_text} is masked but no stored value exists"
                )
            restored[key_text] = stored_map[key_text]
        else:
            restored[key_text] = str(value)
    return restored


__all__ = [
    "InvalidToolHeader",
    "MASK",
    "is_sensitive_header",
    "mask_headers",
    "restore_masked_headers",
]
