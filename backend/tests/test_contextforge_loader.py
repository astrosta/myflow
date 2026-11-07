import pytest
from langchain_core.tools import StructuredTool

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.loader import load_contextforge_tools
from agentkit.tools.contextforge_metadata import is_contextforge_tool


def _tool() -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    return StructuredTool.from_function(_run, name="demo", description="demo")


@pytest.mark.asyncio
async def test_loader_tags_tools_from_the_contextforge_server(monkeypatch) -> None:
    async def fake_load(session, **kwargs):
        assert session is None
        assert kwargs["connection"]["url"] == "http://cf/mcp"
        return [_tool()]

    monkeypatch.setattr("agentkit.contextforge.loader.load_mcp_tools", fake_load)
    config = ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="adm",
        tool_loading="eager",
        search={"enabled": False},
    )
    tools = await load_contextforge_tools(config)
    assert len(tools) == 1
    assert is_contextforge_tool(tools[0])
    assert tools[0].metadata["myflow_mcp"]["server"] == "contextforge"


@pytest.mark.asyncio
async def test_loader_returns_empty_when_disabled() -> None:
    assert await load_contextforge_tools(ContextForgeConfig()) == []