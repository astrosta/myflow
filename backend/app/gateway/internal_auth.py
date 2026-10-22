"""内部认证：**谁能代表某用户**（M9 §2.9）。

IM 通道和调度器不是浏览器。它们走进程内 HTTP 调自己的 Gateway，带一对头：

```
X-MyFlow-Internal-Token: <MYFLOW_INTERNAL_AUTH_TOKEN>
X-MyFlow-Owner-User-Id:  <storage user id>
```

**只有 AuthMiddleware 能把这对头授权成当前用户。** 任何其它代码路径读到
`X-MyFlow-Owner-User-Id` 都必须当普通不可信输入丢掉。

已知边界（部署文档同步）：

> 如果把 `MYFLOW_INTERNAL_AUTH_TOKEN` 下发给了终端（移动 App、浏览器），
> 那个"代表某用户"的头就可以被伪造。正确做法是只有 Gateway 本机 / 通道 worker /
> 调度器持有密钥。

三关（§2.9 的表，缺一不可）：

1. **令牌**：`compare_digest` 相等，否则 401 `unauthenticated`——
   **不要**降级成匿名，否则通道会静默把消息写进 default 桶。
2. **owner 存在**：必须是**存在且启用**的账号的存储 ID，否则 401 `unknown_owner`。
   不允许"先发消息再建号"。
3. **通道允许**：该 owner 确实绑定过这个通道（§5.3 的 `channel_connections`），
   否则 403 `channel_not_bound`。绑定表属于 9D，通过 `register_owner_binding_check()`
   在这一层接上；未注册时（9D 未上线）跳过并打一次 warning。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request

from agentkit.utils import make_safe_user_id
from backend.app.gateway.auth.errors import ChannelNotBound, Unauthenticated, UnknownOwner
from backend.app.gateway.auth.result import AuthKind, AuthResult
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

INTERNAL_TOKEN_HEADER = "x-myflow-internal-token"
OWNER_USER_ID_HEADER = "x-myflow-owner-user-id"

#: 9D 注册的"该 owner 绑定过这个通道吗"。签名：(request, storage_user_id) -> bool。
OwnerBindingCheck = Callable[[Request, str], Awaitable[bool]]

_owner_binding_check: OwnerBindingCheck | None = None


def register_owner_binding_check(check: OwnerBindingCheck | None) -> None:
    """由 9D 的通道层在启动时注册（§5.3）。测试与未上线阶段传 None 摘掉。"""
    global _owner_binding_check
    _owner_binding_check = check


def has_internal_token(request: Request) -> bool:
    return bool(request.headers.get(INTERNAL_TOKEN_HEADER))


def token_matches(candidate: str, settings: AuthSettings) -> bool:
    """列表里任意一个相等即通过：轮换期新旧并存（§2.9）。"""
    if not candidate:
        return False
    return any(secrets.compare_digest(candidate, known) for known in settings.internal_tokens)


async def authenticate_internal(
    request: Request,
    *,
    settings: AuthSettings,
    service: AuthService,
) -> AuthResult:
    if not settings.internal_tokens:
        raise Unauthenticated("internal authentication is not configured on this gateway")

    candidate = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not token_matches(candidate, settings):
        # 不降级成匿名：静默写进 default 桶比 401 难查得多。
        raise Unauthenticated("invalid internal token")

    owner = (request.headers.get(OWNER_USER_ID_HEADER) or "").strip()
    if not owner:
        raise UnknownOwner(f"{OWNER_USER_ID_HEADER} is required for internal callers")

    user = await service.get_user_by_storage_id(owner)
    if user is None or user["disabled"] or user["deleted_at"] is not None:
        raise UnknownOwner(f"unknown or disabled owner: {owner}")

    if _owner_binding_check is not None:
        if not await _owner_binding_check(request, owner):
            raise ChannelNotBound(f"owner {owner} has not bound this channel")
    else:
        _warn_missing_binding_check()

    return AuthResult(
        kind=AuthKind.internal,
        user_id=make_safe_user_id(user["id"]),
        identity=user["id"],
        role=user["system_role"],
        scopes=None,
        needs_setup=False,
    )


_warned_missing_binding_check = False


def _warn_missing_binding_check() -> None:
    global _warned_missing_binding_check
    if _warned_missing_binding_check:
        return
    _warned_missing_binding_check = True
    logger.warning(
        "no owner-binding check registered: internal callers are authorized by token + owner "
        "existence only (channel_connections lands with 9D §5.3)"
    )
