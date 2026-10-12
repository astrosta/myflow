"""会话 cookie 的策略与读写（M9 §2.4）。

**JWT 只在 HttpOnly cookie `access_token` 里传输，不出现在响应 JSON。**
登录响应体只有 `expires_in` 和 `needs_setup`。

四条规则：

1. 只在 HTTPS、受信转发 HTTPS、direct-host localhost HTTP、或操作员显式 opt-in 时**持久化**；
   公网 HTTP 沙箱 URL 降级成 session cookie（关浏览器即没）。
2. `Secure` 只在真的 HTTPS 上打。在纯 HTTP 上打 Secure 等于把 cookie 丢掉。
3. `remember_me` 只影响 `max_age`；密码从不存。
4. CSRF cookie 的 `max_age` 必须与 access cookie **一起 stamp**（同一行代码），
   改密重签时成对更新；logout 清掉所有 auth cookie，且**不再签发 CSRF**。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from backend.app.gateway.auth.client_ip import forwarded_proto

ACCESS_COOKIE_NAME = "access_token"
CSRF_COOKIE_NAME = "csrf_token"

#: direct-host localhost HTTP 也算"能持久化"（本机开发）。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    persist_over_http: bool = False
    domain: str | None = None
    samesite: str = "lax"
    remember_me_max_age: int = 30 * 24 * 3600

    def is_https(self, request: Request) -> bool:
        if (proto := forwarded_proto(request)) is not None:
            return proto == "https"
        return request.url.scheme == "https"

    def is_local_host(self, request: Request) -> bool:
        host = (request.headers.get("host") or request.url.netloc or "").split(":")[0]
        return host.lower() in _LOCAL_HOSTS

    def may_persist(self, request: Request) -> bool:
        return self.is_https(request) or self.is_local_host(request) or self.persist_over_http

    def max_age(self, request: Request, *, remember_me: bool) -> int | None:
        """None = session cookie（关浏览器即没）。"""
        if not remember_me or not self.may_persist(request):
            return None
        return int(self.remember_me_max_age)

    def secure(self, request: Request) -> bool:
        return self.is_https(request)

    def _attributes(self, request: Request) -> dict:
        samesite = "none" if self.samesite == "none" else self.samesite
        return {
            "path": "/",
            "domain": self.domain,
            "secure": True if samesite == "none" else self.secure(request),
            "samesite": samesite,
        }

    def set_session(
        self,
        response: Response,
        request: Request,
        *,
        token: str,
        csrf_token: str | None,
        remember_me: bool = False,
    ) -> None:
        """access + csrf **成对**写。`csrf_token=None` 时不签发 CSRF（logout 用）。"""
        max_age = self.max_age(request, remember_me=remember_me)
        attributes = self._attributes(request)
        response.set_cookie(ACCESS_COOKIE_NAME, token, max_age=max_age, httponly=True, **attributes)
        if csrf_token:
            # 同一个 max_age：两者必须同生共死，否则会出现"有 cookie 没 token"的 403。
            response.set_cookie(CSRF_COOKIE_NAME, csrf_token, max_age=max_age, httponly=False, **attributes)

    def clear(self, response: Response, request: Request) -> None:
        """清掉所有 auth cookie。属性要和写的时候一致，否则浏览器留下孤儿。"""
        attributes = self._attributes(request)
        for name, httponly in ((ACCESS_COOKIE_NAME, True), (CSRF_COOKIE_NAME, False)):
            response.delete_cookie(name, **{**attributes, "httponly": httponly})


def access_token_from(request: Request) -> str | None:
    value = request.cookies.get(ACCESS_COOKIE_NAME)
    return value or None


def csrf_token_from(request: Request) -> str | None:
    value = request.cookies.get(CSRF_COOKIE_NAME)
    return value or None


def apply_session_cookies(
    response: JSONResponse,
    request: Request,
    policy: SessionCookiePolicy,
    *,
    token: str,
    csrf_token: str,
    remember_me: bool = False,
) -> JSONResponse:
    policy.set_session(response, request, token=token, csrf_token=csrf_token, remember_me=remember_me)
    return response
