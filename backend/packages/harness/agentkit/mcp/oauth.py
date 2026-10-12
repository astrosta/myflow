"""OAuth token 端点与自动刷新（M7 §5.5 层 ①）。

token 端点的响应体永远不进日志、不进异常：它带着 access_token，失败时也可能把
client_secret 回显回来。渲染出来的 header 值（<token_type> <access_token>）过
illegal_header_value_reason —— 检查的是**拼好的完整值**，不是分开检查两个字段（§5.6）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import ToolException

from agentkit.mcp.headers import apply_header_overrides, illegal_header_value_reason

logger = logging.getLogger(__name__)

DEFAULT_HEADER = "Authorization"
DEFAULT_EXPIRY_MARGIN_SECONDS = 30.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: float


async def _default_post(url: str, data: Mapping[str, str]) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as client:
        return await client.post(url, data=dict(data))


class OAuthTokenManager:
    """client_credentials / refresh_token，带过期缓存与自动刷新。"""

    def __init__(
        self,
        config: Any,
        *,
        post: Callable[[str, Mapping[str, str]], Awaitable[Any]] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._post = post or _default_post
        self._now = now
        self._token: _Token | None = None

    async def header_value(self) -> str:
        token = await self._current()
        rendered = token.value  # 已经是 "<token_type> <access_token>"
        reason = illegal_header_value_reason(rendered)
        if reason is not None:
            # 不回显值：这个字符串会进模型可见消息、checkpoint 和 trace（§5.6）
            raise ToolException(f"OAuth token from {self._token_url()} is unusable: {reason}")
        return rendered

    async def _current(self) -> _Token:
        token = self._token
        margin = float(getattr(self._config, "expiry_margin_seconds", DEFAULT_EXPIRY_MARGIN_SECONDS))
        if token is not None and self._now() < token.expires_at - margin:
            return token
        self._token = await self._fetch()
        return self._token

    async def _fetch(self) -> _Token:
        url = self._token_url()
        grant = str(getattr(self._config, "type", "client_credentials") or "client_credentials")
        payload: dict[str, str] = {"grant_type": grant}
        for field in ("client_id", "client_secret", "scope", "refresh_token"):
            value = getattr(self._config, field, None)
            if value:
                payload[field] = str(value)
        try:
            response = await self._post(url, payload)
        except Exception as exc:
            # 网络异常的消息里可能带 URL，但不会带 payload；仍然只报类型
            raise ToolException(f"OAuth token request failed: {type(exc).__name__}") from None
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            # 失败响应体可能把 client_secret 回显回来 —— 一个字符都不带出去
            raise ToolException(f"OAuth token request to {url} failed with HTTP {status}")
        try:
            body = response.json()
        except Exception:
            raise ToolException(f"OAuth token response from {url} is not JSON") from None
        if not isinstance(body, Mapping):
            raise ToolException(f"OAuth token response from {url} is not a JSON object")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ToolException(f"OAuth token response from {url} has no access_token")
        token_type = str(body.get("token_type") or "Bearer")
        expires_in = body.get("expires_in")
        seconds = float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600.0
        return _Token(value=f"{token_type} {access_token}", expires_at=self._now() + seconds)

    def _token_url(self) -> str:
        url = getattr(self._config, "token_url", None)
        if not url:
            raise ToolException("OAuth is configured without token_url")
        return str(url)


class OAuthInterceptor:
    """把自动刷新的 token 写进 header。注册在**外层**，后面的层能覆盖它（§5.5）。"""

    def __init__(self, config: Any, *, header: str = DEFAULT_HEADER, manager: OAuthTokenManager | None = None) -> None:
        self._header = header
        self._manager = manager or OAuthTokenManager(config)

    async def __call__(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        value = await self._manager.header_value()
        return await handler(
            request.override(headers=apply_header_overrides(request.headers, {self._header: value}))
        )
