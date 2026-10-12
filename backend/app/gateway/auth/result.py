"""认证结果：AuthMiddleware 写 `request.state.auth`，其余人只读。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthKind(StrEnum):
    session = "session"      # HttpOnly cookie 里的 JWT
    pat = "pat"              # Authorization: Bearer dfp_...
    internal = "internal"    # 通道 / 调度器：内部 token + owner 头（§2.9）
    disabled = "disabled"    # auth.enabled=false（§2.11）
    anonymous = "anonymous"  # 白名单路由上的无凭据请求


@dataclass(frozen=True, slots=True)
class AuthResult:
    """一次请求的认证结果。

    `user_id` 是**存储 ID**（`make_safe_user_id(users.id)`）：中间件解析一次，
    下游（ContextVar、RunCaller、仓储）全部用它，绝不各自再算一遍。
    `identity` 是原始 identity（= `users.id`），只用于授权过滤与审计。
    """

    kind: AuthKind
    user_id: str | None = None
    identity: str | None = None
    role: str | None = None
    scopes: frozenset[str] | None = None   # None = 会话/内部：不按 scope 裁
    needs_setup: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.kind is not AuthKind.anonymous

    @property
    def is_internal(self) -> bool:
        return self.kind is AuthKind.internal


ANONYMOUS = AuthResult(kind=AuthKind.anonymous)
