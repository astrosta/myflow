"""M7 §5.1：工具名前缀、去重、以及源路由只认 metadata。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool

from agentkit.mcp.tools import McpToolNameError, load_server_tools, qualify_tool_name
from agentkit.tools.mcp_metadata import MCP_TOOL_META_KEY, is_mcp_tool, mcp_tool_meta, tag_mcp_tool


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


def test_the_prefix_is_on_by_default(monkeypatch):
    _patch_discovery(monkeypatch, ["search"])

    tools = asyncio.run(load_server_tools("github", {"transport": "stdio"}))

    assert [tool.name for tool in tools] == ["github_search"]


def test_the_prefix_can_be_turned_off(monkeypatch):
    _patch_discovery(monkeypatch, ["search"])

    tools = asyncio.run(
        load_server_tools("github", {"transport": "stdio"}, prefix_enabled=False)
    )

    assert [tool.name for tool in tools] == ["search"]


def test_two_servers_with_the_same_tool_name_do_not_collide(monkeypatch):
    _patch_discovery(monkeypatch, ["search"])

    github = asyncio.run(load_server_tools("github", {"transport": "stdio"}))
    gitlab = asyncio.run(load_server_tools("gitlab", {"transport": "stdio"}))

    names = [tool.name for tool in [*github, *gitlab]]
    assert names == ["github_search", "gitlab_search"]  # 不加前缀会在 M2 去重里被静默丢掉一个


def test_source_routing_uses_the_metadata_even_without_a_prefix(monkeypatch):
    _patch_discovery(monkeypatch, ["search"])

    without_prefix = asyncio.run(
        load_server_tools("github", {"transport": "stdio"}, prefix_enabled=False)
    )[0]
    with_prefix = asyncio.run(load_server_tools("github", {"transport": "stdio"}))[0]

    for tool in (without_prefix, with_prefix):
        meta = mcp_tool_meta(tool)
        assert meta is not None
        assert (meta.server, meta.original_name) == ("github", "search")
        assert is_mcp_tool(tool) is True
        assert tool.metadata[MCP_TOOL_META_KEY] == {"server": "github", "original_name": "search"}


def test_the_original_name_survives_renaming(monkeypatch):
    _patch_discovery(monkeypatch, ["list_issues"])

    tool = asyncio.run(load_server_tools("github", {"transport": "stdio"}))[0]

    assert tool.name == "github_list_issues"
    assert mcp_tool_meta(tool).original_name == "list_issues"  # 不是从前缀反推


def test_a_plain_tool_has_no_mcp_meta():
    tool = _fake_tool("read_file")

    assert mcp_tool_meta(tool) is None
    assert is_mcp_tool(tool) is False


def test_tagging_keeps_other_metadata():
    tool = _fake_tool("search")
    tool.metadata = {"annotations": {"readOnlyHint": True}}

    tag_mcp_tool(tool, server="s", original_name="search")

    assert tool.metadata["annotations"] == {"readOnlyHint": True}


@pytest.mark.parametrize("server", ["bad name", "bad/name", "", "x" * 65, "bad\nname"])
def test_an_unsafe_server_name_is_rejected(server):
    with pytest.raises(McpToolNameError):
        qualify_tool_name(server, "search", prefix_enabled=True)


def test_an_unsafe_server_name_drops_every_tool(monkeypatch):
    _patch_discovery(monkeypatch, ["search"])

    tools = asyncio.run(load_server_tools("bad name", {"transport": "stdio"}))

    assert tools == []