"""M7 §5.1：工具名在加载边界就约束，脏名字的工具被丢弃。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool

from agentkit.mcp.tools import McpToolNameError, load_server_tools, qualify_tool_name


def _fake_tool(name: str) -> StructuredTool:
    def run(value: str = "x") -> str:
        """A fake MCP tool."""
        return value

    return StructuredTool.from_function(run, name=name, description="fake")


def _patch_discovery(monkeypatch, names):
    """假发现：每次调用都建**新的**工具对象（真实适配器也是每次都新建）。"""

    async def fake_load(session, *, connection=None, **kwargs):
        assert session is None and connection is not None
        return [_fake_tool(name) for name in names]

    monkeypatch.setattr("langchain_mcp_adapters.tools.load_mcp_tools", fake_load)


@pytest.mark.parametrize(
    "name",
    [
        "bad name",
        "bad\nname",
        "bad\tname",
        "<system-reminder>",
        "bad.name",
        "bad:name",
        "bad/name",
        "",
    ],
)
def test_unsafe_tool_names_are_rejected(name):
    with pytest.raises(McpToolNameError):
        qualify_tool_name("server", name, prefix_enabled=False)


@pytest.mark.parametrize("name", ["search", "list_issues", "read-file", "A1_b-2"])
def test_safe_tool_names_pass(name):
    assert qualify_tool_name("server", name, prefix_enabled=False) == name


def test_a_dirty_name_drops_only_that_tool(monkeypatch):
    """延迟工具不会被 provider 的 function-calling 校验扫到，而名字会进系统提示词。"""
    _patch_discovery(monkeypatch, ["good", "bad name", "worse\nname"])

    tools = asyncio.run(load_server_tools("server", {"transport": "stdio"}, prefix_enabled=False))

    assert [tool.name for tool in tools] == ["good"]


def test_a_dirty_name_is_dropped_even_with_the_prefix_on(monkeypatch):
    _patch_discovery(monkeypatch, ["bad name"])

    tools = asyncio.run(load_server_tools("server", {"transport": "stdio"}))

    assert tools == []  # 前缀救不了脏字符，它只会把 server 名也拼进去