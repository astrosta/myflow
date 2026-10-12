"""认证门：**默认拒绝**（M9 §2.5）。

不在 public 白名单上的路由一律要求认证——**新加一条路由默认就是受保护的**。
反过来（默认放行、要保护的加装饰器）只要有人忘了加装饰器就是一个洞。

- 存在 cookie 但无效 / 过期 / 用户不存在 / `token_version` 不匹配 → 401，
  **不要**落到业务路由再 500。
- 任何非 public 路由**不能只靠"cookie 存在"放行**，必须严格验证 JWT。

中间件里**不能 raise 了等处理器接**：Starlette 的异常处理器在 ExceptionMiddleware 里，
而它在用户中间件的**内侧**。异常必须在这里变成响应返回。
"""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from agentkit.runtime.user_context import DEFAULT_USER_ID, reset_current_user, set_current_user
from backend.app.gateway.auth.errors import (
    AuthUnavailable,
    SessionRequired,
    SetupRequired,
    Unauthenticated,
)
from backend.app.gateway.internal_auth import authenticate_internal, has_internal_token
from backend.app.gateway.auth.result import ANONYMOUS, AuthKind, AuthResult
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.session_cookie import access_token_from
from backend.app.gateway.auth.settings import AuthSettings, resolve_auth_settings
from backend.app.gateway.error_response import CodedError, coded_error_response

logger = logging.getLogger(__name__)

#: 精确匹配的公开路径（§2.5）。写进模块常量，测试遍历 OpenAPI 断言非白名单都 401。
PUBLIC_EXACT_PATHS = frozenset({
    "/health",
    "/health/ready",
    "/api/v1/auth/login/local",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
    "/api/v1/auth/setup-status",
    "/api/v1/auth/initialize",
    "/api/v1/auth/providers",
})

#: 路由不注册也留着：打过去是 404，不是 401（§2.12 的"不做"）。
PUBLIC_PATH_PREFIXES = (
    "/api/v1/auth/oauth/",
    "/api/v1/auth/callback/",
)

#: `GATEWAY_ENABLE_DOCS=false` 时从白名单拿掉。
DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})

#: 认证配置读不出来也要放行的路径：健康检查是编排系统的探针，不是用户请求。
ALWAYS_OPEN_PATHS = frozenset({"/health", "/health/ready"})

#: `needs_setup=true` 的用户只能做这三件事（§2.13 不变量 3）。
#:
#: **按方法而不只看路径**：`/api/v1/auth/me` 同时有 GET（看自己是谁）和
#: PATCH（改用户名）。刚被管理员重置过密码的人，正事是改密码——先让他去改用户名，
#: 只是把"旧口令还能用"的窗口拖长。
SETUP_ALLOWED_ROUTES = frozenset({
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/change-password"),
    ("POST", "/api/v1/auth/logout"),
})

#: `auth.enabled=false`（§2.11）：固定用户 + 全能力，桶名仍是 `default`。
#:
#: 桶名**故意**不用 "auth-disabled"：M4 时代的数据都在 `users/default/`，
#: 换个名字等于让老用户"数据消失"——这正是 §2.2 警告的那颗雷。
DISABLED_AUTH_RESULT = AuthResult(
    kind=AuthKind.disabled,
    user_id=DEFAULT_USER_ID,
    identity=DEFAULT_USER_ID,
    role="admin",
    scopes=None,
    needs_setup=False,
)

_warned_disabled = False


def _warn_auth_disabled() -> None:
    global _warned_disabled
    if _warned_disabled:
        return
    _warned_disabled = True
    logger.warning(
        "auth.enabled=false: every request runs as the fixed %r user with full capabilities "
        "(single-user demo only; GATEWAY_WORKERS>1 refuses to start)",
        DEFAULT_USER_ID,
    )


def is_public_path(path: str, settings: AuthSettings) -> bool:
    if path in PUBLIC_EXACT_PATHS:
        return True
    if path in DOCS_PATHS:
        return settings.enable_docs
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


class AuthGateMiddleware(BaseHTTPMiddleware):
    """认证结果写 `request.state.auth`，并把存储 ID 绑到 ContextVar。

    ContextVar 是 persistence 那一侧的输入：`resolve_user_id(AUTO)` 读的就是它。
    绑定与解绑必须在同一个任务里成对出现（`finally`），否则 id 会漏到下一个请求。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in ALWAYS_OPEN_PATHS:
            # 健康检查不该因为认证配置坏了而变红：编排系统会把它当成"进程死了"。
            return await call_next(request)

        try:
            settings = resolve_auth_settings(request)
        except Exception as exc:  # noqa: BLE001 —— 读不出来时 fail-closed，但要说真话
            logger.error("auth settings unavailable: %r", exc)
            return coded_error_response(
                request,
                AuthUnavailable(
                    "the gateway cannot load its auth configuration; see the server log"
                ),
            )

        if not settings.enabled:
            _warn_auth_disabled()
            result: AuthResult = DISABLED_AUTH_RESULT
        else:
            public = is_public_path(path, settings)
            try:
                result = await _resolve(request, settings=settings, required=not public) or ANONYMOUS
            except CodedError as exc:
                return coded_error_response(request, exc)
            except Exception:  # noqa: BLE001 —— 未知异常也 fail-closed：401，不是放行
                logger.exception("authentication failed on %s %s", request.method, path)
                return coded_error_response(request, Unauthenticated("authentication failed"))

        if (
            result.needs_setup
            and (request.method, path) not in SETUP_ALLOWED_ROUTES
            and not is_public_path(path, settings)
        ):
            # admin 重置密码之后，用户在拿到新密码之前不该能用旧会话做任何事（§2.13）。
            return coded_error_response(request, SetupRequired("password change required"))

        request.state.auth = result
        token = set_current_user(result.user_id)
        try:
            return await call_next(request)
        finally:
            reset_current_user(token)


async def _resolve(request: Request, *, settings: AuthSettings, required: bool) -> AuthResult | None:
    """凭据解析顺序：Bearer PAT → 内部头 → 会话 cookie → 匿名 / 401。

    **带了 `Authorization` 但无效 → 硬 401，禁止回退到 cookie**（§2.6）：
    否则 CSRF 中间件的"Bearer 就跳过 double-submit"会变成一个洞——
    攻击页面随便写一个坏 header，请求就改走 cookie 会话。
    """
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential.strip():
            raise Unauthenticated("Authorization must be a Bearer token")
        result = await get_auth_service(request).authenticate_pat(credential.strip(), settings=settings)
        if result is None:
            raise Unauthenticated("invalid or expired token")
        return result

    if has_internal_token(request):
        return await authenticate_internal(
            request, settings=settings, service=get_auth_service(request)
        )

    cookie = access_token_from(request)
    if cookie:
        result = await get_auth_service(request).authenticate_session(cookie, settings=settings)
        if result is None:
            if required:
                raise Unauthenticated("invalid or expired session")
            # 公开路由上的陈旧 cookie 只是陈旧：**不能**因此把用户锁在登录页外面。
            # 登录 / 初始化会立刻覆盖它；受保护路由仍然是 401（上面那条）。
            return None
        return result

    if required:
        raise Unauthenticated("authentication required")
    return None


def get_auth_service(request: Request) -> AuthService:
    from backend.app.gateway.deps import get_auth_service as _get

    return _get(request)


def require_session(auth: AuthResult | None) -> AuthResult:
    """只允许**会话**：PAT 打这些路由是 403 `session_required`（§2.12）。"""
    if auth is None or auth.kind is not AuthKind.session:
        raise SessionRequired("this endpoint requires a browser session")
    return auth


__all__ = [
    "DOCS_PATHS",
    "PUBLIC_EXACT_PATHS",
    "PUBLIC_PATH_PREFIXES",
    "SETUP_ALLOWED_ROUTES",
    "AuthGateMiddleware",
    "is_public_path",
    "require_session",
]
