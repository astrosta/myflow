"""M7 §5.4：工具内部的运行上下文只能从 ToolRuntime 读，不在 RunnableConfig 里。

真实适配器工具 + 真实图：上游改了参数注入约定，这条会立刻红。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, TextContent, Tool as McpTool

from agentkit.mcp.tools import _runtime_scope
from fakes.chat_model import FakeChatModel

CAPTURED: dict = {}


class _CaptureInterceptor:
    """短路：不调 handler，所以永远不会 spawn 真的 MCP 服务器。"""

    async def __call__(self, request, handler):
        CAPTURED["runtime"] = request.runtime
        CAPTURED["context"] = getattr(request.runtime, "context", None)
        CAPTURED["server_name"] = request.server_name
        try:
            from langgraph.config import get_config

            CAPTURED["ambient"] = dict(get_config())
        except Exception as exc:  # 没有 ambient 上下文也能跑
            CAPTURED["ambient"] = f"unavailable: {type(exc).__name__}"
        return CallToolResult(content=[TextContent(type="text", text="captured")])


class _ScriptedModel(FakeChatModel):
    """先调一次工具，再收尾。必须是假模型里**支持 bind_tools** 的那个（create_agent 会调）。"""

    responses: list = []
    index: int = 0

    def _result(self) -> ChatResult:
        self.calls += 1
        message = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def _adapter_tool():
    return convert_mcp_tool_to_langchain_tool(
        None,
        McpTool(
            name="whoami",
            description="Who am I",
            inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        connection={"transport": "stdio", "command": "npx", "args": []},
        server_name="demo",
        tool_interceptors=[_CaptureInterceptor()],
    )


def test_an_adapter_tool_sees_the_runtime_context_through_a_real_graph():
    CAPTURED.clear()
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "whoami", "id": "call-1", "args": {"value": "x"}}],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_agent(model=model, tools=[_adapter_tool()])

    async def run():
        # MCP 工具只有 coroutine（async-only），所以图要走 ainvoke
        return await graph.ainvoke(
            {"messages": [HumanMessage(content="hi", id="m1")]},
            config={"configurable": {"thread_id": "t1"}},
            context={"user_id": "u1", "thread_id": "t1"},
        )

    asyncio.run(run())

    assert CAPTURED["context"] == {"user_id": "u1", "thread_id": "t1"}
    assert CAPTURED["server_name"] == "demo"
    assert CAPTURED["runtime"] is not None
    # ✗ 旧的写法在工具调用内部拿到的是 None：
    ambient = CAPTURED["ambient"]
    if isinstance(ambient, dict):
        assert "context" not in ambient or ambient.get("context") is None


def test_the_stdio_scope_reads_user_and_thread_from_the_runtime():
    runtime = SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"}, config={})

    assert _runtime_scope(runtime) == ("u1", "t1")


def test_the_stdio_scope_falls_back_to_configurable_only_for_the_thread():
    runtime = SimpleNamespace(
        context={"user_id": "u1"}, config={"configurable": {"thread_id": "t9"}}
    )

    assert _runtime_scope(runtime) == ("u1", "t9")


def test_a_missing_runtime_yields_an_empty_scope():
    assert _runtime_scope(None) == (None, None)
    assert _runtime_scope(SimpleNamespace(context=None, config=None)) == (None, None)