"""AuthSettings：配置 + 环境变量的**一次**解析（M9 §7）。

中间件与路由都从这里拿设置，不各自读 `os.environ`——那样会出现
"CORS 放行了但 CSRF 不认"这类两套事实的 bug（§2.6）。

解析顺序：`app.state.auth_settings`（测试/嵌入显式钉住）→ `get_app_config().auth`。
配置读不出来时**不静默放行**：调用方按 401 处理（fail-closed）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from fastapi import Request

from agentkit.config import AppConfig, get_app_config
from backend.app.gateway.auth.client_ip import forwarded_proto
from backend.app.gateway.auth.jwt import JwtConfigError, require_secret
from backend.app.gateway.auth.session_cookie import SessionCookiePolicy

CORS_ORIGINS_ENV = "GATEWAY_CORS_ORIGINS"
ENABLE_DOCS_ENV = "GATEWAY_ENABLE_DOCS"


def cors_origins_from_env() -> tuple[str, ...]:
    raw = os.getenv(CORS_ORIGINS_ENV, "")
    return tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


def docs_enabled_from_env() -> bool:
    """生产部署显式设 `GATEWAY_ENABLE_DOCS=false`：docs 从公开白名单里拿掉。"""
    return os.getenv(ENABLE_DOCS_ENV, "true").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class AuthSettings:
    enabled: bool = True
    jwt_secret: str = ""
    jwt_ttl_seconds: int = 86400
    allow_registration: bool = False
    internal_tokens: tuple[str, ...] = ()
    cookie: SessionCookiePolicy = field(default_factory=SessionCookiePolicy)
    max_login_attempts: int = 5
    lockout_seconds: float = 300.0
    #: true = 登录失败说清"没这个邮箱 / 密码错 / 被禁用 / 回收期"（§2.12 的默认，按产品要求打开）
    specific_errors: bool = True
    pat_max_days: int = 90
    max_pats: int = 20
    cors_origins: tuple[str, ...] = ()
    enable_docs: bool = True

    @classmethod
    def from_config(cls, config: AppConfig) -> AuthSettings:
        auth = config.auth
        return cls(
            enabled=auth.enabled,
            jwt_secret=auth.jwt_secret,
            jwt_ttl_seconds=auth.jwt_ttl_seconds,
            allow_registration=auth.allow_registration,
            internal_tokens=auth.effective_internal_tokens(),
            cookie=SessionCookiePolicy(
                persist_over_http=auth.persist_cookies_over_http,
                domain=auth.cookie_domain,
                samesite=auth.cookie_samesite,
                remember_me_max_age=auth.remember_me_max_age_seconds,
            ),
            max_login_attempts=auth.local.max_login_attempts,
            lockout_seconds=auth.local.lockout_seconds,
            specific_errors=auth.local.specific_errors,
            pat_max_days=auth.pat_max_days,
            max_pats=auth.max_pats,
            cors_origins=cors_origins_from_env(),
            enable_docs=docs_enabled_from_env(),
        )

    @classmethod
    def disabled(cls) -> AuthSettings:
        """单人演示（§2.11）。`install_middleware(..., auth_settings=...)` 测试也用这个。"""
        return cls(enabled=False, jwt_secret="", internal_tokens=())

    def with_secret(self, secret: str) -> AuthSettings:
        return replace(self, jwt_secret=secret)

    def validate_for_startup(self) -> None:
        """认证开着就必须有像样的密钥。启动预检直接调它，别等到第一次登录。"""
        if self.enabled:
            require_secret(self.jwt_secret)

    def is_origin_allowed(self, origin: str, request: Request) -> bool:
        candidate = origin.strip().rstrip("/")
        if not candidate:
            return False
        if candidate in self.cors_origins:
            return True
        host = (request.headers.get("host") or request.url.netloc or "").strip()
        if not host:
            return False
        scheme = forwarded_proto(request) or request.url.scheme
        return candidate in {f"{scheme}://{host}", f"http://{host}", f"https://{host}"}


def resolve_auth_settings(request: Request) -> AuthSettings:
    pinned = getattr(request.app.state, "auth_settings", None)
    if isinstance(pinned, AuthSettings):
        return pinned
    return AuthSettings.from_config(get_app_config())


__all__ = [
    "CORS_ORIGINS_ENV",
    "ENABLE_DOCS_ENV",
    "AuthSettings",
    "JwtConfigError",
    "cors_origins_from_env",
    "docs_enabled_from_env",
    "resolve_auth_settings",
]
