"""M7 §2.1：只写 transport: http 的服务器不能落进默认 stdio。"""

from __future__ import annotations

from agentkit.config.extensions_config import ExtensionsConfig
from backend.app.gateway.models.request import McpConfigUpdateRequest


def test_transport_is_read_as_the_type():
    config = ExtensionsConfig.model_validate(
        {"mcpServers": {"remote": {"transport": "http", "url": "https://mcp.example.test"}}}
    )

    server = config.mcp_servers["remote"]
    assert server.type == "http"
    assert server.is_stdio is False


def test_type_wins_when_both_are_written():
    config = ExtensionsConfig.model_validate(
        {"mcpServers": {"remote": {"type": "sse", "transport": "http", "url": "https://x"}}}
    )

    assert config.mcp_servers["remote"].type == "sse"


def test_a_missing_type_is_stdio():
    config = ExtensionsConfig.model_validate({"mcpServers": {"local": {"command": "npx"}}})

    assert config.mcp_servers["local"].is_stdio is True


def test_the_alias_is_not_written_back():
    config = ExtensionsConfig.model_validate(
        {"mcpServers": {"remote": {"transport": "http", "url": "https://x"}}}
    )

    written = config.to_file_dict()["mcpServers"]["remote"]

    assert written["type"] == "http"
    assert "transport" not in written


def test_the_write_model_normalizes_spellings():
    body = McpConfigUpdateRequest.model_validate(
        {"mcpServers": {"remote": {"transport": "HTTP", "url": "https://x"}}}
    )

    written = body.mcp_servers["remote"].to_config_dict()

    assert written["type"] == "http"
    assert "transport" not in written


def test_the_type_wins_in_the_write_model_too():
    body = McpConfigUpdateRequest.model_validate(
        {"mcpServers": {"remote": {"type": "sse", "transport": "http", "url": "https://x"}}}
    )

    assert body.mcp_servers["remote"].to_config_dict()["type"] == "sse"
