"""M9 §2 测试用的最小认证 app：**真**中间件 + **真**路由 + 内存账号表。

不跑 lifespan、不连数据库：认证门只依赖 `app.state.auth_service`，
而业务路由依赖 `app.state.run_services`（这里是内存仓储）。

为什么每个用例都要自己搭 app：`app.state.auth_settings` 是**显式钉住**的，
测试结果不该取决于仓库里有没有 `config.yaml`（§2.11 的 demo 模式同理）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.events.memory import MemoryRunEventStore
from agentkit.runtime.runs import DisconnectMode, RunRecord, RunStatus
from agentkit.persistence.run.memory import MemoryRunStore
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.auth.login_limit import LoginRateLimiter
from backend.app.gateway.auth.routers import router as auth_router
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.session_cookie import ACCESS_COOKIE_NAME, CSRF_COOKIE_NAME, SessionCookiePolicy
from backend.app.gateway.auth.store import MemoryUserStore
from backend.app.gateway.deps import caller_from_request, get_run_services, require_scope
from backend.app.gateway.routers import thread_runs, threads

#: ≥32 字节：Settings.validate_for_startup 会拒绝更短的（RFC 7518 §3.2）。
TEST_SECRET = "test-secret-0123456789abcdef0123456789abcdef"

ADMIN_EMAIL = "admin@example.com"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password"
USER_EMAIL = "user@example.com"
USER_USERNAME = "user"
USER_PASSWORD = "user-password"


def default_username(email: str) -> str:
    """测试里的用户名默认取邮箱 local part——注册/初始化现在两个都要给。"""
    return email.split("@", 1)[0]


def registration_payload(username: str, password: str) -> dict:
    return {"username": username, "password": password}


class _RunManagerStub:
    """只实现路由用到的 `get`：归属过滤在这里做，别人拿到的就是 None。"""

    def __init__(self, rows: dict[str, dict]) -> None:
        self._rows = rows

    async def get(self, run_id: str, *, user_id: str | None) -> RunRecord | None:
        row = self._rows.get(run_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return RunRecord(**row)


def run_row(*, run_id: str, thread_id: str, user_id: str, status: str = "success") -> dict[str, Any]:
    return dict(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id="lead_agent",
        status=RunStatus(status),
        on_disconnect=DisconnectMode.cancel,
        metadata={},
        kwargs={},
        created_at="2026-10-04T00:00:00Z",
        updated_at="2026-10-04T00:00:00Z",
        user_id=user_id,
    )


@dataclass
class Browser:
    """一个"浏览器"：自己的 cookie jar，写请求自动带 `X-CSRF-Token`（§2.6）。"""

    app: FastAPI
    base_url: str = "http://localhost"
    client: TestClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = TestClient(
            self.app, base_url=self.base_url, raise_server_exceptions=False
        )

    @property
    def csrf_token(self) -> str | None:
        return self.client.cookies.get(CSRF_COOKIE_NAME)

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        token = self.csrf_token
        headers = {"X-CSRF-Token": token} if token else {}
        return {**headers, **(extra or {})}

    def get(self, url: str, **kwargs: Any):
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.client.post(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def patch(self, url: str, **kwargs: Any):
        return self.client.patch(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.client.delete(url, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def login(self, username: str, password: str, **extra: Any):
        return self.post(
            "/api/v1/auth/login/local", json={"username": username, "password": password, **extra}
        )

    def register(self, username: str, password: str, **extra: Any):
        return self.post(
            "/api/v1/auth/register",
            json={**registration_payload(username, password), **extra},
        )

    def initialize(self, username: str, password: str, **extra: Any):
        return self.post(
            "/api/v1/auth/initialize",
            json={**registration_payload(username, password), **extra},
        )

    def clear_cookies(self) -> None:
        self.client.cookies.clear()


@dataclass
class AuthHarness:
    app: FastAPI
    client: TestClient
    base_url: str
    store: MemoryUserStore
    service: AuthService
    settings: AuthSettings
    thread_store: MemoryThreadMetaStore
    run_store: MemoryRunStore

    # --- 会话与请求 ---

    @property
    def csrf_token(self) -> str | None:
        return self.client.cookies.get(CSRF_COOKIE_NAME)

    def auth_headers(self) -> dict[str, str]:
        """浏览器 state-changing 请求要带的那只头（§2.6）。"""
        token = self.csrf_token
        return {"X-CSRF-Token": token} if token else {}

    def post(self, url: str, **kwargs: Any):
        headers = {**self.auth_headers(), **(kwargs.pop("headers", None) or {})}
        return self.client.post(url, headers=headers, **kwargs)

    def patch(self, url: str, **kwargs: Any):
        headers = {**self.auth_headers(), **(kwargs.pop("headers", None) or {})}
        return self.client.patch(url, headers=headers, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        headers = {**self.auth_headers(), **(kwargs.pop("headers", None) or {})}
        return self.client.delete(url, headers=headers, **kwargs)

    def clear_cookies(self) -> None:
        self.client.cookies.clear()

    def new_browser(self) -> Browser:
        return Browser(self.app, self.base_url)

    def login_as(self, username: str, password: str) -> Browser:
        """新开一个浏览器并登录——两用户用例必须各有各的 cookie jar。"""
        browser = self.new_browser()
        assert browser.login(username, password).status_code == 200
        return browser

    def replay(self, token: str) -> Browser:
        """另一个浏览器：只带这一枚 access cookie。用来证明"旧令牌真的死了"。"""
        other = self.new_browser()
        other.client.cookies.set(ACCESS_COOKIE_NAME, token)
        return other

    # --- 账号 ---

    async def seed_admin(
        self,
        *,
        email: str = ADMIN_EMAIL,
        username: str | None = None,
        password: str = ADMIN_PASSWORD,
    ) -> dict:
        user, _token, _expires = await self.service.initialize(
            username=username or default_username(email),
            password=password,
            settings=self.settings,
        )
        if email:
            user = await self.store.update(user["id"], email=email)
        return user

    async def seed_user(
        self,
        *,
        email: str = USER_EMAIL,
        username: str | None = None,
        password: str = USER_PASSWORD,
        role: str = "user",
    ) -> dict:
        from backend.app.gateway.auth.password import hash_password

        user = await self.store.create(
            user_id=f"id-{email}",
            email=email,
            username=username or default_username(email),
            password_hash=hash_password(password),
            system_role=role,
        )
        return user

    def login(self, username: str, password: str, **extra: Any):
        return self.client.post(
            "/api/v1/auth/login/local", json={"username": username, "password": password, **extra}
        )

    def register(self, username: str, password: str, **extra: Any):
        return self.client.post(
            "/api/v1/auth/register",
            json={**registration_payload(username, password), **extra},
        )

    def initialize(self, username: str, password: str, **extra: Any):
        return self.client.post(
            "/api/v1/auth/initialize",
            json={**registration_payload(username, password), **extra},
        )


def make_auth_app(
    *,
    enabled: bool = True,
    allow_registration: bool = False,
    secret: str = TEST_SECRET,
    base_url: str = "http://localhost",
    max_login_attempts: int = 5,
    lockout_seconds: float = 300.0,
    remember_me_max_age: int = 30 * 24 * 3600,
    internal_tokens: tuple[str, ...] = (),
    cors_origins: tuple[str, ...] = (),
    enable_docs: bool = True,
    max_pats: int = 20,
    pat_max_days: int = 90,
    specific_errors: bool = True,
    business_routes: bool = True,
    raise_server_exceptions: bool = False,
) -> AuthHarness:
    settings = (
        AuthSettings(
            enabled=True,
            jwt_secret=secret,
            allow_registration=allow_registration,
            internal_tokens=internal_tokens,
            cors_origins=cors_origins,
            enable_docs=enable_docs,
            max_pats=max_pats,
            pat_max_days=pat_max_days,
            cookie=SessionCookiePolicy(remember_me_max_age=remember_me_max_age),
            max_login_attempts=max_login_attempts,
            lockout_seconds=lockout_seconds,
            specific_errors=specific_errors,
        )
        if enabled
        else AuthSettings.disabled()
    )

    store = MemoryUserStore()
    service = AuthService(store, limiter=LoginRateLimiter())
    thread_store = MemoryThreadMetaStore()
    run_store = MemoryRunStore()
    run_manager = _RunManagerStub(run_store._rows)
    services = SimpleNamespace(
        run_manager=run_manager,
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=thread_store, checkpointer=SimpleNamespace()),
        run_store=run_store,
        event_store=MemoryRunEventStore(),
    )

    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=settings)
    install_error_handlers(application)
    application.state.auth_service = service
    application.state.run_services = services
    application.include_router(auth_router, prefix="/api")
    # 管理类路由（`require_scope("admin")`）也要在场：PAT 永不带 admin（§2.15）靠它验证。
    if business_routes:
        application.include_router(threads.router, prefix="/api")
        application.include_router(thread_runs.router, prefix="/api")

    @application.get("/api/admin-probe")
    def admin_probe(
        caller: Annotated[Any, Depends(require_scope("admin"))],
    ) -> dict[str, bool]:
        del caller
        return {"ok": True}

    @application.get("/api/probe")
    def probe(request: Request) -> dict[str, Any]:
        """探针：认证门的输出是什么，一眼看见（没有 response_model，测试专用）。"""
        caller = caller_from_request(request)
        services_seen = get_run_services(request)
        return {
            "user_id": caller.user_id,
            "role": caller.user_role,
            "kind": caller.kind,
            "scopes": sorted(caller.scopes) if caller.scopes is not None else None,
            "is_internal": caller.is_internal,
            "has_services": services_seen is not None,
        }

    client = TestClient(
        application, base_url=base_url, raise_server_exceptions=raise_server_exceptions
    )
    return AuthHarness(
        app=application,
        client=client,
        base_url=base_url,
        store=store,
        service=service,
        settings=settings,
        thread_store=thread_store,
        run_store=run_store,
    )


__all__ = [
    "ADMIN_EMAIL",
    "ADMIN_USERNAME",
    "USER_USERNAME",
    "default_username",
    "registration_payload",
    "Browser",
    "ADMIN_PASSWORD",
    "ACCESS_COOKIE_NAME",
    "AuthHarness",
    "CSRF_COOKIE_NAME",
    "TEST_SECRET",
    "USER_EMAIL",
    "USER_PASSWORD",
    "make_auth_app",
    "run_row",
]