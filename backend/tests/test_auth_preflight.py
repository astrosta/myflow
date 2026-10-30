"""M9 §2.11 / §7：认证的启动预检与配置形状。

`preflight_auth` 是"宁可起不来，也不要起错"的那一类检查：缺密钥、多 worker 还关认证，
都在这里拒绝，而不是等第一次请求出 500 或等数据串桶。
"""

from __future__ import annotations

import pytest

from agentkit.config import AppConfig, AuthConfig, LocalLoginConfig
from backend.app.gateway.app import preflight_auth
from backend.app.gateway.auth.jwt import MIN_SECRET_BYTES, JwtConfigError
from agentkit.config.auth_config import INTERNAL_AUTH_TOKEN_ENV

SECRET = "0123456789abcdef0123456789abcdef"


def test_a_broken_config_says_503_not_unauthenticated(monkeypatch) -> None:
    """配置读不出来是**服务端**的问题，不是"你没登录"。

    报 401 `unauthenticated` 会被前端翻译成"登录状态已失效，请重新登录"——
    用户被送去重新登录，而再登一次还是同一个错。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.gateway.app import install_error_handlers, install_middleware
    from backend.app.gateway.auth import settings as settings_mod

    def _boom():
        raise ValueError("Environment variable $DEEPSEEK_API_KEY is not set")

    monkeypatch.setattr(settings_mod, "get_app_config", _boom)
    application = FastAPI(redirect_slashes=False)
    install_middleware(application)  # 不钉 settings：走"按请求解析配置"那条路径
    install_error_handlers(application)
    client = TestClient(application, raise_server_exceptions=False)

    protected = client.get("/api/threads")
    assert protected.status_code == 503
    assert protected.json()["code"] == "auth_unavailable"

    login = client.post("/api/v1/auth/login/local", json={"username": "a", "password": "x"})
    assert login.status_code == 503
    assert login.json()["code"] == "auth_unavailable"
    assert "$DEEPSEEK_API_KEY" not in login.text, "配置细节不回显给客户端"

    # 健康检查是编排系统的探针：认证配置坏了也不能让它变红（这里是 404，说明放行了）
    assert client.get("/health/ready").status_code == 404


def test_auth_is_enabled_by_default_and_demands_a_long_secret(monkeypatch) -> None:
    """默认值必须是生产形状：忘了配 auth 段不等于"没有认证"。"""
    with pytest.raises(JwtConfigError, match="jwt_secret is required"):
        preflight_auth(AppConfig())  # 默认 enabled=true、secret 为空

    with pytest.raises(JwtConfigError, match="at least"):
        preflight_auth(AppConfig(auth=AuthConfig(enabled=True, jwt_secret="too-short")))


def test_a_proper_secret_passes(monkeypatch) -> None:
    monkeypatch.delenv("GATEWAY_WORKERS", raising=False)

    settings = preflight_auth(AppConfig(auth=AuthConfig(enabled=True, jwt_secret=SECRET)))

    assert settings.enabled is True
    assert len(SECRET) >= MIN_SECRET_BYTES


def test_disabled_auth_with_multiple_workers_refuses_to_start(monkeypatch) -> None:
    """多 worker + 认证关 = 所有 worker 共用一个 default 桶，"多用户"这个前提不成立。"""
    monkeypatch.setenv("GATEWAY_WORKERS", "2")

    with pytest.raises(RuntimeError, match="GATEWAY_WORKERS"):
        preflight_auth(AppConfig(auth=AuthConfig(enabled=False)))


def test_disabled_auth_with_a_single_worker_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "1")

    settings = preflight_auth(AppConfig(auth=AuthConfig(enabled=False)))

    assert settings.enabled is False
    assert settings.jwt_secret == ""


def test_the_env_var_is_merged_into_the_token_list(monkeypatch) -> None:
    """轮换期新配置和旧环境变量要能并存（§2.9）。"""
    monkeypatch.setenv(INTERNAL_AUTH_TOKEN_ENV, "from-env")

    config = AuthConfig(internal_tokens=["new", "from-env"])

    assert config.effective_internal_tokens() == ("new", "from-env")


def test_local_login_bounds() -> None:
    assert LocalLoginConfig().max_login_attempts >= 2
    with pytest.raises(Exception):
        LocalLoginConfig(max_login_attempts=1)
    with pytest.raises(Exception):
        LocalLoginConfig(lockout_seconds=0)


def test_an_unknown_auth_key_is_rejected() -> None:
    """认证配置写错一个键名不该被静默忽略——那是"以为开了其实没开"的来源。"""
    with pytest.raises(Exception):
        AuthConfig(enabled=True, jwt_secrets=SECRET)  # 少了一个字母


def test_self_register_is_accepted_as_an_alias() -> None:
    config = AuthConfig(allow_self_register=True)

    assert config.allow_registration is True
