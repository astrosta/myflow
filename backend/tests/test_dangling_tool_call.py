"""Dangling tool calls get a placeholder beside the AI message; orphan results are dropped."""

import asyncio

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from agentkit.agents.middlewares.dangling_tool_call_middleware import (
    DanglingToolCallMiddleware,
    synthetic_tool_call_id,
)

_INTERRUPTED = "[工具调用被中断，没有返回结果。]"


def _request(*messages: object) -> ModelRequest:
    return ModelRequest(model=object(), messages=list(messages))


def _call(call_id: str, name: str) -> dict:
    return {"id": call_id, "name": name, "args": {}}


def test_synthetic_id_is_stable_for_the_same_triple() -> None:
    assert synthetic_tool_call_id(2, "call", 1) == "myflow_synthetic_tool_call_2_call_1"
    assert synthetic_tool_call_id(2, "call", 1) == synthetic_tool_call_id(2, "call", 1)


def test_interrupted_call_is_paired_immediately_after_its_ai_message() -> None:
    ai = AIMessage(content="", tool_calls=[_call("c1", "bash")])
    human = HumanMessage(content="继续")
    patched = DanglingToolCallMiddleware()._build_patched_messages([ai, human])
    assert patched is not None
    assert patched[0].tool_calls[0]["id"] == "c1"
    assert isinstance(patched[1], ToolMessage)
    assert patched[1].tool_call_id == "c1"
    assert patched[1].status == "error"
    assert _INTERRUPTED in patched[1].content
    assert patched[2] is human


def test_empty_id_is_synthesized_the_same_way_on_a_second_wrap() -> None:
    ai = AIMessage(content="", tool_calls=[_call("", "bash")])
    middleware = DanglingToolCallMiddleware()
    first = middleware._build_patched_messages([ai])
    second = middleware._build_patched_messages([ai])
    expected = synthetic_tool_call_id(0, "call", 0)
    assert first[0].tool_calls[0]["id"] == expected
    assert second[0].tool_calls[0]["id"] == expected
    assert first[1].tool_call_id == expected


def test_empty_name_becomes_unknown_tool() -> None:
    ai = AIMessage(content="", tool_calls=[_call("c1", "")])
    patched = DanglingToolCallMiddleware()._build_patched_messages([ai])
    assert patched[0].tool_calls[0]["name"] == "unknown_tool"
    assert patched[1].name == "unknown_tool"
    assert "工具名" in patched[1].content


def test_orphan_tool_message_is_dropped() -> None:
    orphan = ToolMessage(content="leftover", tool_call_id="gone", name="bash")
    human = HumanMessage(content="hi")
    patched = DanglingToolCallMiddleware()._build_patched_messages([orphan, human])
    assert patched == [human]


def test_parallel_empty_ids_are_not_guessed_when_a_result_is_missing() -> None:
    ai = AIMessage(content="", tool_calls=[_call("", "bash"), _call("", "bash")])
    survivor = ToolMessage(content="one", tool_call_id="", name="bash")
    patched = DanglingToolCallMiddleware()._build_patched_messages([ai, survivor])
    placeholders = [message for message in patched if isinstance(message, ToolMessage)]
    assert len(placeholders) == 2
    assert all(_INTERRUPTED in message.content for message in placeholders)
    assert "one" not in "".join(message.content for message in placeholders)


def test_parallel_empty_ids_pair_in_order_when_every_call_has_a_result() -> None:
    ai = AIMessage(content="", tool_calls=[_call("", "bash"), _call("", "bash")])
    first = ToolMessage(content="one", tool_call_id="", name="bash")
    second = ToolMessage(content="two", tool_call_id="", name="bash")
    patched = DanglingToolCallMiddleware()._build_patched_messages([ai, first, second])
    assert [message.content for message in patched[1:]] == ["one", "two"]
    assert patched[1].tool_call_id == synthetic_tool_call_id(0, "call", 0)
    assert patched[2].tool_call_id == synthetic_tool_call_id(0, "call", 1)


def test_write_file_recovery_truncates_the_parser_error() -> None:
    error = "q" * 600
    ai = AIMessage(
        content="",
        invalid_tool_calls=[{"id": "bad", "name": "write_file", "args": "not-json", "error": error}],
    )
    patched = DanglingToolCallMiddleware()._build_patched_messages([ai])
    placeholder = patched[1]
    assert "不是合法 JSON" in placeholder.content
    assert "q" * 500 in placeholder.content
    assert "q" * 501 not in placeholder.content


def test_before_model_replaces_the_whole_list_and_the_next_pass_is_clean() -> None:
    ai = AIMessage(content="", tool_calls=[_call("c1", "bash")])
    middleware = DanglingToolCallMiddleware()
    update = middleware.before_model({"messages": [ai]}, None)
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == REMOVE_ALL_MESSAGES
    stored = update["messages"][1:]
    assert isinstance(stored[1], ToolMessage)
    assert middleware.before_model({"messages": stored}, None) is None
    assert middleware._build_patched_messages(stored) is None


def test_wrap_model_call_hands_the_patched_transcript_to_the_handler() -> None:
    ai = AIMessage(content="", tool_calls=[_call("c1", "bash")])
    seen: dict = {}

    def handler(request: ModelRequest) -> str:
        seen["messages"] = request.messages
        return "ok"

    assert DanglingToolCallMiddleware().wrap_model_call(_request(ai), handler) == "ok"
    assert isinstance(seen["messages"][1], ToolMessage)
    assert seen["messages"][1].tool_call_id == "c1"


def test_async_wrap_patches_the_same_way() -> None:
    async def scenario() -> None:
        seen: dict = {}

        async def handler(request: ModelRequest) -> str:
            seen["count"] = len(request.messages)
            return "ok"

        ai = AIMessage(content="", tool_calls=[_call("c1", "bash")])
        result = await DanglingToolCallMiddleware().awrap_model_call(_request(ai), handler)
        assert result == "ok"
        assert seen["count"] == 2

    asyncio.run(scenario())
