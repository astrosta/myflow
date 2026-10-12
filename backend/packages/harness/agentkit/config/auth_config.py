"""`auth` 段（M9 §7）。默认值就是生产形状：认证**开着**，注册**关着**。

```yaml
auth:
  enabled: true                 # 单人演示才关；关了以后所有数据进 default 桶（§2.11）
  jwt_secret: $AUTH_JWT_SECRET  # HS256 签名密钥
  jwt_ttl_seconds: 86400
  allow_registration: false     # §2.13：自助注册要显式打开
  internal_tokens: []           # 与 DEER_FLOW_INTERNAL_AUTH_TOKEN 合并，支持 [new, old] 轮换
  local:
    max_login_attempts: 5       # 热重载；最小 2（单次失败不得锁 IP，§2.5）
    lockout_seconds: 300
```
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

#: 通道 / 调度器代表用户时带的那只头（§2.9）。轮换期允许新旧并存。
INTERNAL_AUTH_TOKEN_ENV = "DEER_FLOW_INTERNAL_AUTH_TOKEN"


class LocalLoginConfig(BaseModel):
    """本地账号登录限速（§2.5）。计数器是**进程内** dict：多 worker 下近似。"""

    model_config = ConfigDict(extra="forbid")

    max_login_attempts: int = Field(
        default=5,
        ge=2,
        le=1000,
        description="同一 TCP peer IP 的失败次数上限。最小 2：单次失败不得锁 IP。",
    )
    lockout_seconds: float = Field(default=300.0, gt=0, description="锁定时长；到期自动放行")
    specific_errors: bool = Field(
        default=True,
        description=(
            "登录失败是否指出**具体**原因（account_not_found / wrong_password / "
            "account_disabled / account_deleted）。默认 true：用户点一次就知道该去注册还是"
            "该找回密码。设为 false 时全部收敛成 401 invalid_credentials —— "
            "代价是用户不知道为什么失败，收益是别人无法探测某个邮箱有没有注册。"
        ),
    )


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = Field(
        default=True,
        description=(
            "全局认证门。false 仅用于单人演示（§2.11）：AuthMiddleware 注入固定用户，"
            "所有数据进 default 桶；GATEWAY_WORKERS>1 时启动直接拒绝。"
        ),
    )
    jwt_secret: str = Field(
        default="",
        description=(
            "HS256 签名密钥。enabled=true 且为空时启动拒绝——"
            "$AUTH_JWT_SECRET 展开不到会先报错，不要写死默认值。"
        ),
    )
    jwt_ttl_seconds: int = Field(default=86400, gt=0, description="access token 有效期；没有 refresh token")
    allow_registration: bool = Field(
        default=False,
        validation_alias=AliasChoices("allow_registration", "allow_self_register"),
        description="自助注册开关。关掉时 POST /api/v1/auth/register 是 403 registration_closed。",
    )
    internal_tokens: list[str] = Field(
        default_factory=list,
        description=(
            "通道 / 调度器服务间认证的共享密钥（§2.9）。写成 [new, old] 就能不停机轮换："
            "新值先上线，旧值下一版删掉。"
        ),
    )
    persist_cookies_over_http: bool = Field(
        default=False,
        description="操作员显式 opt-in：公网 HTTP 上也持久化 cookie（默认降级成 session cookie）。",
    )
    cookie_domain: str | None = Field(default=None, description="跨子域共享会话时才需要")
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        default="lax",
        description="SameSite。none 必须同时 secure=true（浏览器规矩，不是我们的）",
    )
    remember_me_max_age_seconds: int = Field(
        default=30 * 24 * 3600,
        gt=0,
        description="remember_me 只影响 max_age；密码从不存（§2.4）",
    )
    pat_max_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="PAT 最长有效期（§2.15）。到期即 401，不给宽限。",
    )
    max_pats: int = Field(default=20, ge=1, description="每用户 PAT 数量上限，超出 409 too_many_pats")
    local: LocalLoginConfig = Field(default_factory=LocalLoginConfig)

    def effective_internal_tokens(self) -> tuple[str, ...]:
        """配置列表 + `DEER_FLOW_INTERNAL_AUTH_TOKEN`，去重且保持顺序。"""
        tokens: list[str] = []
        for token in (*self.internal_tokens, os.getenv(INTERNAL_AUTH_TOKEN_ENV, "")):
            if token and token not in tokens:
                tokens.append(token)
        return tuple(tokens)
