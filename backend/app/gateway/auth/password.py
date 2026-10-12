"""密码哈希与口令策略（M9 §2.4 / §2.15）。

bcrypt，cost 12。三条纪律：

- **明文不进日志、不进数据库、不进错误 detail。** 只在这里出现一次。
- 上限按 **字节** 算：bcrypt 5.x 对超过 72 字节的口令直接抛 `ValueError`，
  而 128 个非 ASCII 字符可以有 300+ 字节。这里把它提前成一句人话，
  而不是让它在 bcrypt 里炸成 500。
- **错误要具体**：用户要知道"短了几位 / 超了几个字节"，而不是一句"密码不合法"。
  所以策略错误直接是带 `code` 的域异常，路由层不需要再翻译一遍。
"""

from __future__ import annotations

import bcrypt

from backend.app.gateway.auth.errors import (
    InvalidPassword,
    PasswordTooLong,
    PasswordTooShort,
)

#: bcrypt 的硬上限（字节）。超过就必须先降级成 hash，而不是截断。
MAX_PASSWORD_BYTES = 72

#: 新口令的最小长度（字符）。登录时不校验长度，只做 hash 比对。
MIN_PASSWORD_CHARS = 8

_BCRYPT_ROUNDS = 12


def validate_password_policy(raw: str) -> str:
    """返回原口令，或抛一个**说清原因**的 422。

    登录路径不调它（登录只比对 hash）：拿一个 100 字节的口令去登录，
    应该得到"密码不对"，而不是"密码太长"。
    """
    if not isinstance(raw, str) or not raw:
        raise InvalidPassword("password must be a non-empty string")
    if len(raw) < MIN_PASSWORD_CHARS:
        raise PasswordTooShort(
            f"password must be at least {MIN_PASSWORD_CHARS} characters (got {len(raw)})"
        )
    size = len(raw.encode("utf-8"))
    if size > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes in UTF-8 (got {size} bytes); "
            "non-ASCII characters take 2-4 bytes each"
        )
    return raw


def hash_password(raw: str) -> str:
    validate_password_policy(raw)
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(raw: str, hashed: str | None) -> bool:
    """错口令、空 hash、坏 hash 都是 False——不要把异常漏到路由层变成 500。"""
    if not raw or not hashed:
        return False
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
