"""M7 §5.5：凭据四层优先级，全部 fail-closed；非法值绝不回显。"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

from agentkit.config.extensions_config import ExtensionsConfig, McpServerConfig
from agentkit.mcp.context_headers import ContextHeadersInterceptor
from agentkit.mcp.interceptors import build_interceptors
from agentkit.mcp.oauth import OAuthInterceptor, OAuthTokenManager
from agentkit.mcp.user_scoped_auth import UserScopedAuthInterceptor

USER_AUTH = {"users": {"u1": {"X-Api-Key": "$MCP_TEST_KEY"}}}
CONTEXT_MAP = {"X-Api-Key": "API_KEY", "Authorization": "TOKEN"}


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _server(**overrides) -> McpServerConfig:
    fields = {"type": "http", "url": "https://mcp.example.test/mcp"}
    fields.update(overrides)
    return McpServerConfig.model_validate(fields)


def _request(*, headers=None, context=None, user_id="u1"):
    """假的 MCPToolCallRequest：override 保留 runtime，和适配器的 dataclasses.replace 一致。"""
    ctx: dict = {"user_id": user_id} if user_id is not None else {}
    if context:
        ctx.update(context)

    def build(current_headers):
        return SimpleNamespace(
            headers=current_headers,
            runtime=SimpleNamespace(context=ctx),
            override=lambda **kw: build(kw.get("headers", current_headers)),
        )

    return build(headers)


async def _run_chain(chain, request, *, execute=None):
    handler = execute or (lambda req: _ok(req))

    for interceptor in reversed(list(chain)):
        handler = _wrap(interceptor, handler)
    return await handler(request)


def _wrap(interceptor, inner):
    async def call(request):
        return await interceptor(request, inner)

    return call


async def _ok(request):
    return request


class _Recorder:
    def __init__(self) -> None:
        self.requests: list = []

    async def __call__(self, request):
        self.requests.append(request)
        return request


def test_the_three_layers_are_registered_outermost_first():
    server = _server(
        **{
            "headers": {"X-Static": "static"},
            "oauth": {"token_url": "https://auth.example.test/token"},
            "user_auth": USER_AUTH,
            "headers_from_context": CONTEXT_MAP,
        }
    )

    chain = build_interceptors(server, config=ExtensionsConfig(), transport="http")

    assert [type(item).__name__ for item in chain] == [
        "OAuthInterceptor",  # ① 自动刷新（最外层）
        "UserScopedAuthInterceptor",  # ② 按用户
        "ContextHeadersInterceptor",  # ③ 按请求（最靠近传输层，赢得最终 header）
    ]


def test_ops_interceptors_are_appended_closest_to_the_transport():
    config = ExtensionsConfig.model_validate({"mcpInterceptors": ["json:dumps"]})

    chain = build_interceptors(_server(), config=config, transport="http")

    assert chain == [json.dumps]


def test_an_unresolvable_ops_interceptor_is_skipped(caplog):
    config = ExtensionsConfig.model_validate({"mcpInterceptors": ["nope:missing", "json:loads"]})

    with caplog.at_level(logging.WARNING, logger="agentkit.mcp.interceptors"):
        chain = build_interceptors(_server(), config=config, transport="http")

    assert chain == [json.loads]


def test_credentials_layers_are_skipped_on_stdio(caplog):
    server = _server(
        **{
            "type": "stdio",
            "command": "npx",
            "user_auth": USER_AUTH,
            "headers_from_context": CONTEXT_MAP,
        }
    )

    with caplog.at_level(logging.WARNING, logger="agentkit.mcp.interceptors"):
        chain = build_interceptors(server, config=ExtensionsConfig(), transport="stdio")

    assert chain == []
    assert any("stdio" in record.getMessage() for record in caplog.records)


def test_the_per_request_layer_beats_the_per_user_layer(monkeypatch):
    monkeypatch.setenv("MCP_TEST_KEY", "user-key")
    server = _server(**{"user_auth": USER_AUTH, "headers_from_context": {"X-Api-Key": "API_KEY"}})
    chain = build_interceptors(server, config=ExtensionsConfig(), transport="http")
    base = {"X-Static": "static", "X-Api-Key": "static-key"}

    final = asyncio.run(
        _run_chain(
            chain, _request(headers=base, context={"secrets": {"API_KEY": "per-request-key"}})
        )
    )

    assert final.headers == {"X-Static": "static", "X-Api-Key": "per-request-key"}
    assert base["X-Api-Key"] == "static-key"  # 输入没被就地改


def test_a_missing_user_mapping_fails_closed():
    interceptor = UserScopedAuthInterceptor(
        McpServerConfig.model_validate({"user_auth": USER_AUTH}).user_auth
    )
    recorder = _Recorder()

    with pytest.raises(ToolException) as raised:
        asyncio.run(interceptor(_request(user_id="someone-else"), recorder))

    assert "someone-else" in str(raised.value)
    assert recorder.requests == []  # 没有静默回落到发现凭据


def test_passthrough_allows_a_missing_value():
    server = _server(**{"user_auth": {"users": {}, "on_missing": "passthrough"}})
    chain = build_interceptors(server, config=ExtensionsConfig(), transport="http")
    recorder = _Recorder()

    result = asyncio.run(chain[0](_request(), recorder))

    assert result is not None
    assert len(recorder.requests) == 1


def test_missing_context_secrets_name_only_the_missing_keys():
    interceptor = ContextHeadersInterceptor({"Authorization": "TOKEN", "X-Api-Key": "API_KEY"})

    with pytest.raises(ToolException) as raised:
        asyncio.run(interceptor(_request(context={"secrets": {"TOKEN": "t"}}), _Recorder()))

    message = str(raised.value)
    assert "API_KEY" in message
    assert "TOKEN" not in message
    assert "missing required secret(s)" in message


def test_an_illegal_credential_never_echoes_its_value(caplog):
    secret = "Bearer\nsuper-secret-token"
    interceptor = ContextHeadersInterceptor({"Authorization": "TOKEN"})
    recorder = _Recorder()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ToolException) as raised:
            asyncio.run(
                interceptor(_request(context={"secrets": {"TOKEN": secret}}), recorder)
            )

    assert "super-secret-token" not in str(raised.value)
    assert "super-secret-token" not in caplog.text
    assert "header value contains non-ASCII or control characters" in str(raised.value)
    assert recorder.requests == []


def test_an_illegal_credential_ignores_passthrough():
    """非法值无视 on_missing=passthrough：那是"缺值"的开关，不是"脏值也能送"的开关。"""
    interceptor = ContextHeadersInterceptor({"Authorization": "TOKEN"}, on_missing="passthrough")

    with pytest.raises(ToolException):
        asyncio.run(
            interceptor(
                _request(context={"secrets": {"TOKEN": "bad\nvalue"}}), _Recorder()
            )
        )


def test_an_illegal_user_credential_never_echoes_its_value(monkeypatch):
    monkeypatch.setenv("MCP_TEST_KEY", "user\nsecret")
    interceptor = UserScopedAuthInterceptor(
        McpServerConfig.model_validate({"user_auth": USER_AUTH}).user_auth
    )

    with pytest.raises(ToolException) as raised:
        asyncio.run(interceptor(_request(), _Recorder()))

    assert "user\nsecret" not in str(raised.value)
    assert "invalid credential for X-Api-Key" in str(raised.value)


def test_oauth_tokens_are_fetched_once_and_refreshed_when_stale():
    calls: list[tuple] = []
    now = {"value": 100.0}

    async def post(url, data):
        calls.append((url, dict(data)))
        return _Response(
            200, {"access_token": f"token-{len(calls)}", "token_type": "Bearer", "expires_in": 60}
        )

    config = McpServerConfig.model_validate(
        {
            "oauth": {
                "token_url": "https://auth.example.test/token",
                "client_id": "cid",
                "client_secret": "shhh",
                "scope": "mcp",
            }
        }
    ).oauth
    manager = OAuthTokenManager(config, post=post, now=lambda: now["value"])

    async def run():
        first = await manager.header_value()
        second = await manager.header_value()
        now["value"] += 100.0
        third = await manager.header_value()
        return first, second, third

    first, second, third = asyncio.run(run())

    assert (first, second, third) == ("Bearer token-1", "Bearer token-1", "Bearer token-2")
    assert calls[0][1]["grant_type"] == "client_credentials"
    assert calls[0][1]["client_secret"] == "shhh"
    assert calls[0][1]["scope"] == "mcp"


def test_oauth_failures_never_echo_the_client_secret():
    async def post(url, data):
        return _Response(401, {"error": "invalid_client", "client_secret": data["client_secret"]})

    config = McpServerConfig.model_validate(
        {
            "oauth": {
                "token_url": "https://auth.example.test/token",
                "client_id": "cid",
                "client_secret": "shhh",
            }
        }
    ).oauth
    manager = OAuthTokenManager(config, post=post)

    with pytest.raises(ToolException) as raised:
        asyncio.run(manager.header_value())

    assert "401" in str(raised.value)
    assert "shhh" not in str(raised.value)


def test_oauth_checks_the_rendered_value_not_the_parts():
    async def post(url, data):
        return _Response(200, {"access_token": "abc\ndef", "token_type": "Bearer"})

    config = McpServerConfig.model_validate(
        {"oauth": {"token_url": "https://auth.example.test/token"}}
    ).oauth
    interceptor = OAuthInterceptor(config, manager=OAuthTokenManager(config, post=post))
    recorder = _Recorder()

    with pytest.raises(ToolException) as raised:
        asyncio.run(interceptor(_request(), recorder))

    assert "abc" not in str(raised.value)
    assert recorder.requests == []


def test_the_oauth_layer_sets_the_header_when_everything_is_fine():
    async def post(url, data):
        return _Response(200, {"access_token": "abc", "token_type": "Bearer"})

    config = McpServerConfig.model_validate(
        {"oauth": {"token_url": "https://auth.example.test/token"}}
    ).oauth
    interceptor = OAuthInterceptor(config, manager=OAuthTokenManager(config, post=post))

    result = asyncio.run(interceptor(_request(headers={"X": "1"}), _ok))

    assert result.headers == {"X": "1", "Authorization": "Bearer abc"}


def test_oauth_without_a_token_url_fails_closed():
    config = McpServerConfig.model_validate({"oauth": {}}).oauth
    manager = OAuthTokenManager(config)

    with pytest.raises(ToolException, match="token_url"):
        asyncio.run(manager.header_value())
