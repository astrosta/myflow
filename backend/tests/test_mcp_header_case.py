"""M7 §5.6：header 名大小写与非法值。"""

from __future__ import annotations

import pytest

from agentkit.mcp.client import McpConfigRejected, build_server_params, validate_static_headers
from agentkit.mcp.headers import (
    apply_header_overrides,
    duplicate_header_spellings,
    illegal_header_value_reason,
)
from agentkit.config.extensions_config import McpServerConfig


def test_case_only_conflicts_collapse_into_one_key():
    """静态写 authorization、注入写 Authorization：两个都到达 httpx 时，服务端单值访问器
    拿到的是静态那个 —— 优先级被静默反转。注入之后只允许剩一个键。"""
    merged = apply_header_overrides({"authorization": "static"}, {"Authorization": "injected"})

    assert merged == {"Authorization": "injected"}
    assert len(merged) == 1


def test_case_only_conflicts_collapse_the_other_way_round_too():
    merged = apply_header_overrides({"Authorization": "static"}, {"authorization": "injected"})

    assert merged == {"authorization": "injected"}
    assert len(merged) == 1


def test_unrelated_headers_are_kept():
    merged = apply_header_overrides({"X-Trace": "1"}, {"Authorization": "t"})

    assert merged == {"X-Trace": "1", "Authorization": "t"}


def test_duplicate_static_spellings_are_rejected_at_load():
    assert duplicate_header_spellings({"authorization": "a", "Authorization": "b"}) == (
        "Authorization",
        "authorization",
    )
    with pytest.raises(McpConfigRejected, match="different spellings"):
        validate_static_headers({"authorization": "a", "Authorization": "b"})


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Bearer abc", None),
        ("Bearer\nevil", "header value contains non-ASCII or control characters"),
        ("Bearer\revil", "header value contains non-ASCII or control characters"),
        ("Bearer abc ", "header value has leading or trailing whitespace"),
        (" Bearer abc", "header value has leading or trailing whitespace"),
        ("Bearer\tcafé", "header value contains non-ASCII or control characters"),
    ],
)
def test_illegal_header_value_reason(value, expected):
    assert illegal_header_value_reason(value) == expected


def test_the_reason_never_contains_the_value():
    secret = "Bearer\nsuper-secret-value"

    reason = illegal_header_value_reason(secret)

    assert reason is not None
    assert "secret" not in reason
    assert "\n" not in reason


def test_static_header_values_are_checked_without_echoing_them():
    with pytest.raises(McpConfigRejected) as raised:
        validate_static_headers({"Authorization": "Bearer\nleaked-value"})

    assert "leaked-value" not in str(raised.value)


def test_build_server_params_merges_static_headers_only_once():
    server = McpServerConfig.model_validate(
        {"type": "http", "url": "https://mcp.example.test/mcp", "headers": {"authorization": "static"}}
    )

    params = build_server_params(server, server_name="remote")

    assert params["headers"] == {"authorization": "static"}
    assert params["transport"] == "http"
    assert len(params["headers"]) == 1
