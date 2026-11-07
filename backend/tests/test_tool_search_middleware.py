from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest

from agentkit.agents.middlewares.tool_search_middleware import ToolSearchMiddleware
from agentkit.agents.thread_state import PROMOTED_MAX_ENTRIES, merge_promoted


class StubTool:
    def __init__(self, name: str, *, contextforge: bool = False) -> None:
        self.name = name
        self.metadata = {"myflow_contextforge": {"original_name": name}} if contextforge else {}


def _request(tools, state):
    return ModelRequest(
        model=object(),
        messages=[],
        state=dict(state),
        tools=list(tools),
        runtime=SimpleNamespace(context={}),
    )


def _visible(middleware, tools, state) -> list[str]:
    seen = {}

    def handler(request):
        seen["request"] = request
        return "ok"

    middleware.wrap_model_call(_request(tools, state), handler)
    return [tool.name for tool in seen["request"].tools]


def test_deferred_hides_contextforge_tools_until_promoted() -> None:
    tools = [StubTool("read_file"), StubTool("search_tools"), StubTool("github_search", contextforge=True)]
    middleware = ToolSearchMiddleware(deferred=True)
    assert _visible(middleware, tools, {}) == ["read_file", "search_tools"]
    assert _visible(middleware, tools, {"promoted": ["github_search"]}) == [
        "read_file",
        "search_tools",
        "github_search",
    ]


def test_eager_keeps_everything() -> None:
    tools = [StubTool("read_file"), StubTool("github_search", contextforge=True)]
    assert _visible(ToolSearchMiddleware(deferred=False), tools, {}) == [
        "read_file",
        "github_search",
    ]


def test_promoted_is_capped_but_keeps_newest_entries() -> None:
    existing = [str(index) for index in range(PROMOTED_MAX_ENTRIES)]
    merged = merge_promoted(existing, ["new"])
    assert len(merged) == PROMOTED_MAX_ENTRIES
    assert merged[-1] == "new"
