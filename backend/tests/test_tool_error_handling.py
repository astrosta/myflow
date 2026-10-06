"""Tool exceptions become error ToolMessages; successful results get tool metadata."""

import asyncio

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from agentkit.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from agentkit.agents.middlewares.tool_result_meta import TOOL_META_KEY


def _request(name: str | None = "bash", call_id: str | None = "c1") -> ToolCallRequest:
    tool_call: dict = {"args": {}}
    if name is not None:
        tool_call["name"] = name
    if call_id is not None:
        tool_call["id"] = call_id
    return ToolCallRequest(tool_call=tool_call, tool=None, state={}, runtime=object())


def test_exception_becomes_a_stamped_error_message() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise ValueError("connection timeout")

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    assert isinstance(message, ToolMessage)
    assert message.name == "bash"
    assert message.tool_call_id == "c1"
    assert message.status == "error"
    assert message.content == (
        "Error: Tool 'bash' failed with ValueError: connection timeout. "
        "Continue with available context, or choose an alternative tool."
    )
    meta = message.additional_kwargs[TOOL_META_KEY]
    assert meta["source"] == "exception"
    assert meta["error_type"] == "transient"
    assert meta["status"] == "error"


def test_missing_name_and_id_use_stable_literals() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("boom")

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(name=None, call_id=None), handler)
    assert message.name == "unknown_tool"
    assert message.tool_call_id == "missing_tool_call_id"
    assert "unknown_tool" in message.content


def test_empty_exception_text_uses_the_class_name() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("   ")

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    assert "RuntimeError: RuntimeError." in message.content


def test_long_detail_is_truncated_before_redaction_markers() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("x" * 600)

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    detail = message.content.split("RuntimeError: ", 1)[1]
    assert detail.startswith("x" * 497 + "...")
    assert "x" * 498 not in detail


def test_secret_assignment_is_removed_from_the_detail() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("failed secrets=sk-live-secret")

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    assert "sk-live-secret" not in message.content
    assert "secrets=[REDACTED]" in message.content


def test_graph_bubble_up_propagates() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise GraphBubbleUp("stop")

    with pytest.raises(GraphBubbleUp):
        ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)


def test_cancellation_propagates() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)


def test_successful_tool_message_is_normalized() -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="all good", tool_call_id="c1", name="bash")

    message = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    assert message.content == "all good"
    assert message.additional_kwargs[TOOL_META_KEY]["status"] == "success"
    assert message.additional_kwargs[TOOL_META_KEY]["source"] == "content_analysis"


def test_command_result_stamps_only_the_matching_tool_message() -> None:
    other = ToolMessage(content="Error: 401 unauthorized", tool_call_id="other", name="bash", status="error")
    match = ToolMessage(content="ok", tool_call_id="c1", name="bash")

    def handler(request: ToolCallRequest) -> Command:
        return Command(update={"messages": [other, match], "n": 1}, goto="agent")

    command = ToolErrorHandlingMiddleware().wrap_tool_call(_request(), handler)
    assert command.goto == "agent"
    assert command.update["n"] == 1
    assert TOOL_META_KEY not in command.update["messages"][0].additional_kwargs
    assert command.update["messages"][1].additional_kwargs[TOOL_META_KEY]["status"] == "success"


def test_async_exception_uses_the_same_message() -> None:
    async def scenario() -> None:
        async def handler(request: ToolCallRequest) -> ToolMessage:
            raise ValueError("401 unauthorized")

        message = await ToolErrorHandlingMiddleware(app_config=None).awrap_tool_call(_request(), handler)
        assert message.tool_call_id == "c1"
        assert message.additional_kwargs[TOOL_META_KEY]["error_type"] == "auth"
        assert message.additional_kwargs[TOOL_META_KEY]["source"] == "exception"

    asyncio.run(scenario())
