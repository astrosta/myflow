"""M9 §2：身份只有一个权威来源。

- `app.gateway.auth_middleware` 写 `request.state.auth`（认证结果）。
- `app.gateway.deps.caller_from_request` 读它，转成 `RunCaller`。
- `agentkit.runtime.user_context` 是存储桶的唯一解析入口。
- **业务代码不读 cookie、不读 JWT、不读 users 表**（admin 管理接口除外）。
"""

from backend.app.gateway.auth.result import ANONYMOUS, AuthKind, AuthResult
from backend.app.gateway.auth.settings import AuthSettings, resolve_auth_settings

__all__ = [
    "ANONYMOUS",
    "AuthKind",
    "AuthResult",
    "AuthSettings",
    "resolve_auth_settings",
]
