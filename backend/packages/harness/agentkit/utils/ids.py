import re
import hashlib

_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_\-]")

def validate_user_id(value: str) -> str:
    if not _SAFE_USER_ID_RE.match(value):
        raise ValueError(f"Invalid user ID: {value}, only letters, numbers, underscores, and hyphens are allowed")
    return value

def make_safe_user_id(raw: str) -> str:
    sanitized = _UNSAFE_CHARS_RE.sub("-", raw)
    if raw == sanitized:
        return raw

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized}-{digest}"