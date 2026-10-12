"""拦截器组装顺序：oauth → user_auth → context → 运维文件（M7 §5.5）。

**先注册的在外层，后注册的更靠近传输层、赢得最终 header。**
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agentkit.config.extensions_config import ExtensionsConfig, McpServerConfig
from agentkit.mcp.context_headers import ContextHeadersInterceptor
from agentkit.mcp.oauth import OAuthInterceptor
from agentkit.mcp.user_scoped_auth import UserScopedAuthInterceptor

logger = logging.getLogger(__name__)


def build_interceptors(
    server: McpServerConfig, *, config: ExtensionsConfig, transport: str
) -> list[Any]:
    chain: list[Any] = []
    if transport == "stdio":
        # stdio 用 env，没有 header 可说：三层凭据层跳过 + warning（§5.5 的表）
        if server.oauth is not None or server.user_auth is not None or server.headers_from_context:
            logger.warning(
                "MCP credentials layers (oauth / user_auth / headers_from_context) are ignored "
                "on stdio servers; use env instead"
            )
    else:
        if server.oauth is not None:
            chain.append(OAuthInterceptor(server.oauth))  # ① 自动刷新
        if server.user_auth is not None:
            chain.append(UserScopedAuthInterceptor(server.user_auth))  # ② 按用户映射
        if server.headers_from_context:
            chain.append(ContextHeadersInterceptor(server.headers_from_context))  # ③ 按请求
    chain.extend(resolve_ops_interceptors(config.mcp_interceptors))
    return chain


def resolve_ops_interceptors(paths: Sequence[str]) -> list[Any]:
    """运维文件里的 mcpInterceptors：会 import 代码，所以 Gateway 不提供写它的 API（§2）。"""
    from agentkit.reflection import resolve_variable

    resolved: list[Any] = []
    for path in paths or ():
        try:
            interceptor = resolve_variable(path, None)
        except Exception as exc:
            logger.warning("Skipping mcpInterceptors entry %s: %s", path, exc)
            continue
        if not callable(interceptor):
            logger.warning("Skipping mcpInterceptors entry %s: it is not callable", path)
            continue
        resolved.append(interceptor)
    return resolved
