"""CSRF：double-submit + Origin 检查（M9 §2.6）。

- 服务端设 `csrf_token` cookie（非 HttpOnly，浏览器 JS 要读它）
- 浏览器 state-changing 请求带同值 `X-CSRF-Token`
- `secrets.compare_digest` 比较；需要 CSRF 的方法只有 POST / PUT / DELETE / PATCH

两条例外：

- **auth bootstrap**（login / register / initialize / logout）不要求 double-submit
  （首次调用时浏览器还没有 token），但**必须**校验 `Origin`，拒 hostile Origin，
  避免 login CSRF / session fixation。
- **Bearer** 跳过 cookie 对，Origin 检查照跑。无效 Bearer 由 AuthMiddleware 硬 401，
  绝不回退 cookie——这就是这条例外不会变成洞的原因。

`GATEWAY_CORS_ORIGINS`：`CORSMiddleware` 和本中间件**读同一个变量**（§2.6）。
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from backend.app.gateway.auth.errors import AuthUnavailable, CsrfFailed, OriginNotAllowed
from backend.app.gateway.auth.session_cookie import access_token_from, csrf_token_from
from backend.app.gateway.auth.settings import resolve_auth_settings
from backend.app.gateway.error_response import coded_error_response

logger = logging.getLogger(__name__)

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

CSRF_HEADER = "x-csrf-token"

#: 首次调用时浏览器还没有 token，所以不要求 double-submit，只校验 Origin。
BOOTSTRAP_PATHS = frozenset({
    "/api/v1/auth/login/local",
    "/api/v1/auth/register",
    "/api/v1/auth/initialize",
    "/api/v1/auth/logout",
})


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in STATE_CHANGING_METHODS:
            return await call_next(request)

        try:
            settings = resolve_auth_settings(request)
        except Exception as exc:  # noqa: BLE001 —— fail-closed，但报 503 而不是"你没登录"
            logger.error("csrf settings unavailable: %r", exc)
            return coded_error_response(
                request,
                AuthUnavailable("the gateway cannot load its auth configuration; see the server log"),
            )

        if not settings.enabled:
            # 没有会话 cookie 就没有 CSRF 可言；单人演示模式不拦（§2.11）。
            return await call_next(request)

        path = request.url.path
        origin = request.headers.get("origin")
        if origin and not settings.is_origin_allowed(origin, request):
            return coded_error_response(
                request, OriginNotAllowed(f"origin {origin!r} is not allowed")
            )

        if path in BOOTSTRAP_PATHS:
            return await call_next(request)

        if request.headers.get("authorization"):
            # Bearer 不用 cookie 对。AuthMiddleware 紧接着会硬 401 无效令牌。
            return await call_next(request)

        cookie = csrf_token_from(request)
        header = request.headers.get(CSRF_HEADER)
        if not cookie and not access_token_from(request):
            # 既没有 CSRF cookie 也没有 access cookie：这是匿名请求，
            # 认证门会 401，这里没有可保护的东西。
            return await call_next(request)
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            return coded_error_response(request, CsrfFailed("missing or mismatched CSRF token"))
        return await call_next(request)
