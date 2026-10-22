"""per-user 凭据：用户 → header 映射（M7 §5.5 层 ②）。

**fail-closed 是默认**：映射里没有当前用户、或解析出空值，就抛 ToolException 并只点名
缺失的键。静默回落到发现凭据 = 一个租户用另一个凭据的权限在跑。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import ToolException

from agentkit.config.app_config import AppConfig
from agentkit.config.extensions_config import McpUserAuthConfig
from agentkit.mcp.headers import apply_header_overrides, illegal_header_value_reason
from agentkit.sandbox.runtime import runtime_user_bucket

logger = logging.getLogger(__name__)

ON_MISSING_PASSTHROUGH = "passthrough"


class UserScopedAuthInterceptor:
    """user_auth.users[user_id] 是 {header: value}，值可以写 $ENV（只展开值，不展开键）。"""

    def __init__(self, user_auth: McpUserAuthConfig, *, on_missing: str | None = None) -> None:
        self._users = {str(user): dict(entry) for user, entry in (user_auth.users or {}).items()}
        self._on_missing = on_missing or user_auth.on_missing

    async def __call__(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        runtime = getattr(request, "runtime", None)
        user_id = runtime_user_bucket(runtime) if runtime is not None else "default"
        entry = self._users.get(str(user_id))
        if not entry:
            if self._on_missing == ON_MISSING_PASSTHROUGH:
                return await handler(request)
            raise ToolException(
                f"missing MCP credentials for user {user_id!r}: user_auth.users has no entry"
            )
        overrides: dict[str, str] = {}
        missing: list[str] = []
        for header, raw in entry.items():
            value = AppConfig.resolve_env_variables(raw)
            if not isinstance(value, str) or not value:
                missing.append(str(header))
                continue
            reason = illegal_header_value_reason(value)
            if reason is not None:
                raise ToolException(f"invalid credential for {header}: {reason}")
            overrides[str(header)] = value
        if missing and self._on_missing != ON_MISSING_PASSTHROUGH:
            raise ToolException(f"missing required credential(s): {', '.join(sorted(missing))}")
        if not overrides:
            return await handler(request)
        return await handler(
            request.override(headers=apply_header_overrides(request.headers, overrides))
        )
