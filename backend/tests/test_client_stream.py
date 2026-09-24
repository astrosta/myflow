import logging

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from agentkit.client import StreamEvent, translate_stream

USAGE = {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}


def _events(*items) -> list[StreamEvent]:
    return list(translate_stream(items))


def _of_type(events: list[StreamEvent], event_type: str) -> list[StreamEvent]:
    return [event for event in events if event.type == event_type]


def test_translate_stream_emits_the_four_public_event_types() -> None:
    events = _events(
        ("custom", {"type": "progress"}),
        ("messages", (AIMessageChunk(id="a", content="hi"), {})),
        ("values", {"messages": [], "title": None, "summary_text": None, "artifacts": []}),
    )
    assert [event.type for event in events] == ["custom", "messages-tuple", "values", "end"]
    assert events[-1].data["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_ai_text_is_delta_and_values_does_not_synthesize_it_again() -> None:
    events = _events(
        ("messages", (AIMessageChunk(id="a", content="hel"), {})),
        ("messages", (AIMessageChunk(id="a", content="lo"), {})),
        (
            "values",
            {
                "messages": [AIMessage(id="a", content="hello")],
                "summary_text": None,
            },
        ),
    )
    texts = [event.data["content"] for event in _of_type(events, "messages-tuple")]
    assert texts == ["hel", "lo"]
    snapshot = _of_type(events, "values")[0].data["messages"]
    assert snapshot[0]["content"] == "hello"


def test_usage_is_counted_once_per_message_id() -> None:
    events = _events(
        ("messages", (AIMessageChunk(id="a", content="ab", usage_metadata=USAGE), {})),
        ("messages", (AIMessageChunk(id="a", content="cd", usage_metadata=USAGE), {})),
        (
            "values",
            {
                "messages": [AIMessage(id="a", content="abcd", usage_metadata=USAGE)],
                "summary_text": None,
            },
        ),
    )
    assert events[-1].data["usage"] == USAGE
    usages = [event.data.get("usage") for event in _of_type(events, "messages-tuple")]
    assert usages == [USAGE, None]


def test_summary_text_is_present_on_every_snapshot() -> None:
    events = _events(
        {"messages": [], "summary_text": "draft"},
        {"messages": [], "summary_text": "draft"},
        {"messages": [], "summary_text": None},
    )
    assert [event.data["summary_text"] for event in _of_type(events, "values")] == [
        "draft",
        "draft",
        None,
    ]


def test_seen_ids_keep_messages_tuple_count_flat_across_cumulative_snapshots() -> None:
    messages: list[AIMessage] = []
    items = []
    for index in range(10):
        messages.append(AIMessage(id=f"m{index}", content=f"text-{index}"))
        items.append({"messages": list(messages), "summary_text": None})
    tuples = _of_type(_events(*items), "messages-tuple")
    assert len(tuples) == 10
    assert [event.data["content"] for event in tuples] == [f"text-{index}" for index in range(10)]


def test_changed_kwargs_without_text_emit_an_empty_content_event() -> None:
    events = _events(
        ("messages", (AIMessageChunk(id="a", content="", additional_kwargs={"trace": "1"}), {})),
        ("messages", (AIMessageChunk(id="a", content="", additional_kwargs={"trace": "1"}), {})),
        ("messages", (AIMessageChunk(id="a", content="", additional_kwargs={"trace": "1", "n": 2}), {})),
    )
    tuples = _of_type(events, "messages-tuple")
    assert [event.data["content"] for event in tuples] == ["", ""]
    assert tuples[0].data["additional_kwargs"] == {"trace": "1"}
    assert tuples[1].data["additional_kwargs"] == {"n": 2}


def test_single_stream_mode_without_mode_prefix_is_values() -> None:
    events = _events({"messages": [], "title": "thread", "summary_text": "s"})
    assert events[0].type == "values"
    assert events[0].data["title"] == "thread"
    assert events[0].data["summary_text"] == "s"
    assert events[0].data["artifacts"] == []


def test_tool_call_and_tool_result_are_emitted_once() -> None:
    tool_calls = [{"name": "fake", "args": {}, "id": "c1", "type": "tool_call"}]
    call = AIMessage(id="a", content="", tool_calls=tool_calls)
    result = ToolMessage(id="t", content="ok", tool_call_id="c1", name="fake")
    events = _events(
        ("messages", (AIMessageChunk(id="a", content="", tool_calls=tool_calls), {})),
        ("messages", (result, {})),
        ("values", {"messages": [call, result], "summary_text": None}),
        ("values", {"messages": [call, result], "summary_text": None}),
    )
    tuples = _of_type(events, "messages-tuple")
    assert [event.data["type"] for event in tuples] == ["ai", "tool"]
    assert tuples[0].data["tool_calls"] == tool_calls
    assert tuples[1].data["content"] == "ok"


def test_values_synthesis_emits_tool_calls_before_text() -> None:
    message = AIMessage(
        id="a",
        content="done",
        tool_calls=[{"name": "fake", "args": {}, "id": "c1", "type": "tool_call"}],
        additional_kwargs={"trace": "1"},
    )
    tuples = _of_type(
        _events(("values", {"messages": [message], "summary_text": None})),
        "messages-tuple",
    )
    assert [event.data.get("tool_calls") is not None for event in tuples] == [True, False]
    assert tuples[0].data["additional_kwargs"] == {"trace": "1"}
    assert "additional_kwargs" not in tuples[1].data
    assert tuples[1].data["content"] == "done"


def test_messages_mode_emits_text_before_tool_calls() -> None:
    tool_calls = [{"name": "fake", "args": {}, "id": "c1", "type": "tool_call"}]
    tuples = _of_type(
        _events(
            (
                "messages",
                (
                    AIMessageChunk(
                        id="a",
                        content="look",
                        tool_calls=tool_calls,
                        additional_kwargs={"trace": "1"},
                    ),
                    {"langgraph_node": "agent"},
                ),
            )
        ),
        "messages-tuple",
    )
    assert tuples[0].data["content"] == "look"
    assert tuples[0].data["additional_kwargs"] == {"trace": "1"}
    assert tuples[1].data["tool_calls"] == tool_calls
    assert "additional_kwargs" not in tuples[1].data


def test_v2_stream_envelopes_read_type_and_data() -> None:
    tool_calls = [{"name": "fake", "args": {}, "id": "c1", "type": "tool_call"}]
    events = _events(
        {"type": "custom", "ns": (), "data": {"type": "progress"}},
        {
            "type": "messages",
            "ns": (),
            "data": (AIMessageChunk(id="a", content="hi", tool_calls=tool_calls), {}),
        },
        {
            "type": "values",
            "ns": (),
            "data": {
                "messages": [AIMessage(id="a", content="hi", tool_calls=tool_calls)],
                "title": "thread",
                "summary_text": "s",
            },
            "interrupts": (),
        },
    )
    assert [event.type for event in events] == [
        "custom",
        "messages-tuple",
        "messages-tuple",
        "values",
        "end",
    ]
    assert events[0].data == {"type": "progress"}
    assert events[1].data["content"] == "hi"
    assert events[2].data["tool_calls"] == tool_calls
    snapshot = events[3].data
    assert snapshot["title"] == "thread"
    assert snapshot["summary_text"] == "s"
    assert snapshot["messages"][0]["content"] == "hi"


def test_values_only_human_message_is_synthesized_once() -> None:
    human = HumanMessage(id="h", content="hello")
    events = _events(
        {"messages": [human], "summary_text": None},
        {"messages": [human], "summary_text": None},
    )
    tuples = _of_type(events, "messages-tuple")
    assert len(tuples) == 1
    assert tuples[0].data["type"] == "human"
    assert tuples[0].data["content"] == "hello"


def test_fake_tool_progress_is_a_custom_stream_event() -> None:
    from agentkit.tools.builtin.fake_tool import fake_tool

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([fake_tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    call = AIMessage(
        content="",
        tool_calls=[{"name": "fake_tool", "args": {"message": "hi"}, "id": "c1", "type": "tool_call"}],
    )
    custom = _of_type(
        _events(*app.stream({"messages": [call]}, stream_mode=["custom", "values"])),
        "custom",
    )
    assert custom == [StreamEvent(type="custom", data={"type": "fake_tool_progress"})]


def test_custom_chunk_without_type_is_discarded(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentkit.client"):
        events = _events(("custom", {"progress": 1}), ("custom", "bare"), ("custom", {"type": ""}))
    assert _of_type(events, "custom") == []
    assert events[-1].type == "end"
    assert any(record.levelno == logging.WARNING for record in caplog.records)
