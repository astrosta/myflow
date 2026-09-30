import hashlib
import re

_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_\-]")
_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

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

def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not _THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ValueError(f"Invalid thread ID: {thread_id}, thread ID must match ^[A-Za-z0-9_\-]{1,64}$")
    
    return thread_id

