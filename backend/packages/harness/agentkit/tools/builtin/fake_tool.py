from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.utils import emit_custom_event


@tool
def fake_tool(message: str, runtime: ToolRuntime) -> str:
    """Return the message unchanged after a progress event."""
    emit_custom_event({"type": "fake_tool_progress"}, writer=runtime.stream_writer)
    return message
