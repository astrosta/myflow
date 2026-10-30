"""认证路由（M9 §2.12）。prefix=`/v1/auth`，create_app 再加 `/api`。

这些路由在白名单上（§2.5），所以**不再套** `require_scope`；只有 `/me` 与
`change-password` 要求"已登录，且必须是**会话**"——PAT 打这两条是 403 `session_required`。

三条容易写错的纪律：

- **响应体里没有 token**：JWT 只进 HttpOnly cookie（§2.4）。测试钉死这一点。
- **失败不区分** "用户名不存在"和"密码错"：同一个 detail（§2.12）。
- **cookie 只在 `session_cookie.py` 里写/清**：这里只调用策略，不自己拼属性。
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from backend.app.gateway.auth.client_ip import client_ip
from backend.app.gateway.auth.models import (
    ChangePasswordRequest,
    InitializeRequest,
    LoginRequest,
    LoginResponse,
    OkResponse,
    ProvidersResponse,
    RegisterRequest,
    SetupStatusResponse,
    UsernameUpdateRequest,
    UserResponse,
    user_to_wire,
)
from backend.app.gateway.auth.result import AuthResult
from backend.app.gateway.auth.service import AuthService
from backend.app.gateway.auth.settings import AuthSettings, resolve_auth_settings
from backend.app.gateway.deps import get_auth_service, get_session_auth

router = APIRouter(prefix="/v1/auth", tags=["auth"])

Settings = Annotated[AuthSettings, Depends(resolve_auth_settings)]
Service = Annotated[AuthService, Depends(get_auth_service)]
SessionUser = Annotated[AuthResult, Depends(get_session_auth)]


def _issue_cookies(
    response: Response,
    request: Request,
    settings: AuthSettings,
    *,
    token: str,
    remember_me: bool = False,
) -> None:
    """access + csrf **成对**写（§2.4）。"""
    settings.cookie.set_session(
        response, request, token=token, csrf_token=secrets.token_urlsafe(32), remember_me=remember_me
    )


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status(service: Service) -> SetupStatusResponse:
    """公开：前端据此决定进 /setup 还是 /login。"""
    return SetupStatusResponse(needs_setup=await service.needs_setup())


@router.post("/initialize", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def initialize(
    body: InitializeRequest,
    request: Request,
    response: Response,
    service: Service,
    settings: Settings,
) -> UserResponse:
    """**只在没有 admin 时**可用；已有 admin 再打就是 409 `admin_already_exists`。

    用户名 / 口令不合规时，服务层直接抛**具体**的 422（`invalid_username` /
    `password_too_short` …）——这里不再提前拦一道，否则用户拿到的是
    pydantic 的 `Request validation failed`。
    """
    user, token, _expires_in = await service.initialize(
        username=body.username, password=body.password, settings=settings
    )
    _issue_cookies(response, request, settings, token=token)
    return UserResponse(**user_to_wire(user))


@router.post("/login/local", response_model=LoginResponse)
async def login_local(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Service,
    settings: Settings,
) -> LoginResponse:
    """登录响应体只有 `expires_in` 和 `needs_setup`——**没有 token 字符串**。

    登录只认 **username + password**。
    """
    user, token, expires_in = await service.login_local(
        username=body.username,
        password=body.password,
        peer_ip=client_ip(request),
        settings=settings,
    )
    _issue_cookies(response, request, settings, token=token, remember_me=body.remember_me)
    return LoginResponse(expires_in=expires_in, needs_setup=bool(user["needs_setup"]))


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    service: Service,
    settings: Settings,
) -> UserResponse:
    """公开；`auth.allow_registration` 为假时 403 `registration_closed`。

    用户名被占 → 409 `username_taken`。
    """
    user, token, _expires_in = await service.register(
        username=body.username, password=body.password, settings=settings
    )
    _issue_cookies(response, request, settings, token=token)
    return UserResponse(**user_to_wire(user))


@router.post("/logout", response_model=OkResponse)
async def logout(request: Request, response: Response, settings: Settings) -> OkResponse:
    """公开：有会话就作废（清 cookie），没有也 200。**不再签发 CSRF**（§2.4）。"""
    settings.cookie.clear(response, request)
    return OkResponse()


@router.get("/providers", response_model=ProvidersResponse)
async def providers() -> ProvidersResponse:
    """OIDC 不在本阶段（§12）；这里只说本地账号开着。"""
    return ProvidersResponse()


@router.get("/me", response_model=UserResponse)
async def me(auth: SessionUser, service: Service) -> UserResponse:
    """只认**会话**：PAT 打这条是 403 `session_required`（§2.15 的路由白名单）。"""
    return UserResponse(**user_to_wire(await service.require_user(auth.identity or "")))


@router.patch("/me", response_model=UserResponse)
async def update_me(body: UsernameUpdateRequest, auth: SessionUser, service: Service) -> UserResponse:
    """改**自己**的用户名（§2.12）。仅会话；PAT 打这条是 403 `session_required`。

    三条刻意的约定：

    - **不动令牌**：改用户名不是撤销事件（§2.13 的失效矩阵），当前会话继续有效，
      别的设备也不用重新登录。
    - **不换桶**：桶名派生自 `users.id`，不是用户名（§2.2）——改完还是同一批对话。
    - 撞名是 409 `username_taken`、形状不对是 422 `invalid_username`（说清是哪一条）。
    """
    user = await service.set_username(
        actor_id=auth.identity or "",
        user_id=auth.identity or "",
        username=body.username,
    )
    return UserResponse(**user_to_wire(user))


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    auth: SessionUser,
    service: Service,
    settings: Settings,
) -> OkResponse:
    """改密 = 撤销**所有**会话（`token_version += 1`），**包括自己这一枚**。

    cookie 在这里清掉，用户必须重新登录一次——这比"悄悄换一枚新 token"更难写错。

    顺序：先校验当前口令（401 `invalid_credentials`），再校验新口令的策略
    （422 `password_too_short` / `password_too_long`）。先报"新口令太短"而把
    "当前口令打错了"藏着，用户会以为改密成功了。
    """
    await service.change_password(
        user_id=auth.identity or "",
        current_password=body.current_password,
        new_password=body.new_password,
    )
    settings.cookie.clear(response, request)
    return OkResponse()
