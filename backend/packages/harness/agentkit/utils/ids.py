import hashlib
import re
from uuid import uuid4

_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_\-]")
_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

def validate_user_id(value: str) -> str:
    if not _SAFE_USER_ID_RE.match(value):
        raise ValueError(f"Invalid user ID: {value}, only letters, numbers, underscores, and hyphens are allowed")
    return value

def make_safe_user_id(raw: str) -> str:
    """**原始 identity → 存储 ID**（M9 §2.2）。唯一允许进文件路径的形状。

    identity 可以是邮箱之类的任意非空字符串，直接拿去拼目录会出问题
    （斜杠、`..`、大小写、超长）。已经安全的 id 原样通过，避免无谓改桶；
    不安全的字符替换成 `-`，再补 16 位 digest——碰撞靠 digest，不靠截断。
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("user_id must be a non-empty string.")
    sanitized = _UNSAFE_CHARS_RE.sub("-", raw)
    if raw == sanitized:
        return raw

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized}-{digest}"

def new_run_id() -> str:
    """32 hex chars. Fits ``runs.run_id`` (String(64)) and `^[A-Za-z0-9_\\-]+$`."""
    return uuid4().hex


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not _THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ValueError(
            f"Invalid thread ID: {thread_id}, thread ID must match ^[A-Za-z0-9_\\-]{{1,64}}$"
        )
    
    return thread_id
