from types import SimpleNamespace

from langchain_core.tools import StructuredTool

from agentkit.contextforge.cache import invalidate_contextforge_cache, set_contextforge_snapshot
from agentkit.tools.contextforge_metadata import is_contextforge_tool, tag_contextforge_tool
from agentkit.tools.registry import get_available_tools


def _tool(name: str, owner: str) -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    tool = StructuredTool.from_function(_run, name=name, description=f"{name} tool")
    return tag_contextforge_tool(tool, original_name=name, owner=owner)


def test_tool_registry_only_returns_the_callers_contextforge_tools() -> None:
    invalidate_contextforge_cache("ownership-test")
    mine = _tool("mine", "u1")
    other = _tool("other", "u2")
    set_contextforge_snapshot(tools=(mine, other), entries=())
    config = SimpleNamespace(
        tools=[],
        sandbox=SimpleNamespace(run_code=SimpleNamespace(enabled=False)),
        skills=SimpleNamespace(search=SimpleNamespace(enabled=False)),
        contextforge=SimpleNamespace(enabled=True),
    )

    try:
        tools = get_available_tools(app_config=config, user_id="u1")
        names = {tool.name for tool in tools if is_contextforge_tool(tool)}
        assert names == {"mine"}
    finally:
        invalidate_contextforge_cache("ownership-test")
