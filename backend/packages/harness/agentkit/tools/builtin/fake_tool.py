from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.utils.custom_events import emit_custom_event


@tool
def fake_tool(message: str, runtime: ToolRuntime) -> str:
    """Return the message unchanged after a progress event.

    M1 stand-in for real tools. Exercises the tool loop and the custom-event exit.
    """
    emit_custom_event({"type": "fake_tool_progress"}, writer=runtime.stream_writer)
    return message
