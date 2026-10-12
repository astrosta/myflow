"""access token 的签发与校验（M9 §2.4）。

声明只有三个：`sub`（users.id）、`tv`（token_version）、`exp`。
**没有 refresh token**：过期就重新登录，`remember_me` 只延长 cookie 的 max_age。

`tv` 是唯一的服务端撤销机制：改密 / admin 重置把 `users.token_version += 1`，
所有旧 cookie 的下一次请求就 401。不要靠缩短 exp 假装能踢人。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt

ALGORITHM = "HS256"

#: HMAC-SHA256 的密钥长度建议（RFC 7518 §3.2）。短于它 PyJWT 会告警，我们直接拒绝。
MIN_SECRET_BYTES = 32


class JwtConfigError(RuntimeError):
    """密钥缺失或过短。启动预检与签发路径共用。"""


def require_secret(secret: str) -> str:
    if not secret:
        raise JwtConfigError(
            "auth.jwt_secret is required when auth.enabled=true; "
            "set it in config.yaml (jwt_secret: $AUTH_JWT_SECRET) or turn auth off for a demo"
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise JwtConfigError(
            f"auth.jwt_secret must be at least {MIN_SECRET_BYTES} bytes; "
            "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return secret


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: str
    token_version: int
    expires_at: int


def encode_access_token(
    *,
    user_id: str,
    token_version: int,
    secret: str,
    ttl_seconds: int,
    now: int | None = None,
) -> tuple[str, int]:
    """返回 `(token, expires_in)`。`expires_in` 是登录响应**唯一**允许出现的凭据信息。"""
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + int(ttl_seconds)
    token = jwt.encode(
        {"sub": user_id, "tv": int(token_version), "exp": expires_at, "iat": issued_at},
        require_secret(secret),
        algorithm=ALGORITHM,
    )
    return token, int(ttl_seconds)


def decode_access_token(token: str, *, secret: str, now: int | None = None) -> TokenClaims | None:
    """签名 / exp / 形状都不对就 None。存在 cookie 但无效 → 401，不是匿名。"""
    if not token or not secret:
        return None
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"], "verify_exp": True},
            leeway=0,
        )
    except jwt.InvalidTokenError:
        return None
    subject = payload.get("sub")
    version = payload.get("tv")
    expires_at = payload.get("exp")
    if not isinstance(subject, str) or not subject:
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not isinstance(expires_at, int):
        return None
    if now is not None and expires_at <= now:
        return None
    return TokenClaims(subject=subject, token_version=version, expires_at=expires_at)
