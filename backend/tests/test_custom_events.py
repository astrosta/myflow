import asyncio
import logging
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter
from typing_extensions import TypedDict

from agentkit.utils import aemit_custom_event, emit_custom_event

PAYLOAD = {"type": "sync_tick", "n": 1}
ASYNC_PAYLOAD = {"type": "async_tick", "n": 1}


class _State(TypedDict):
    n: int


def _sync_node(state: _State, *, writer: StreamWriter) -> dict[str, int]:
    emit_custom_event(PAYLOAD, writer=writer)
    return {"n": state["n"]}


async def _async_node(state: _State, *, writer: StreamWriter) -> dict[str, int]:
    await aemit_custom_event(ASYNC_PAYLOAD, writer=writer)
    return {"n": state["n"]}


def _compile(node: Any) -> Any:
    graph = StateGraph(_State)
    graph.add_node("tick", node)
    graph.add_edge(START, "tick")
    graph.add_edge("tick", END)
    return graph.compile()


def _custom_events(graph: Any, payload: dict[str, Any], *, async_node: bool) -> list[dict[str, Any]]:
    async def collect_chunks() -> list[Any]:
        return [chunk async for chunk in graph.astream({"n": 0}, stream_mode="custom")]

    async def collect_events() -> list[dict[str, Any]]:
        return [
            event
            async for event in graph.astream_events({"n": 0}, version="v2")
            if event["event"] == "on_custom_event"
        ]

    if async_node:
        chunks = asyncio.run(collect_chunks())
    else:
        chunks = list(graph.stream({"n": 0}, stream_mode="custom"))
    assert chunks == [payload]
    return asyncio.run(collect_events())


@pytest.mark.parametrize(
    "payload",
    [{}, {"type": ""}, {"type": None}, {"type": 1}, "nope"],
)
def test_emit_rejects_payload_without_a_string_type(payload: Any) -> None:
    seen: list[Any] = []
    with pytest.raises(ValueError, match="type"):
        emit_custom_event(payload, writer=seen.append)
    assert seen == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"type": ""}, {"type": None}, {"type": 1}, "nope"],
)
def test_aemit_rejects_payload_without_a_string_type(payload: Any) -> None:
    seen: list[Any] = []

    async def send() -> None:
        await aemit_custom_event(payload, writer=seen.append)

    with pytest.raises(ValueError, match="type"):
        asyncio.run(send())
    assert seen == []


def test_writer_error_propagates_and_skips_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def dispatch(name: str, data: Any, *, config: Any = None) -> None:
        called.append(name)

    monkeypatch.setattr("agentkit.utils.custom_events.dispatch_custom_event", dispatch)
    error = RuntimeError("writer failed")

    def writer(_payload: Any) -> None:
        raise error

    with pytest.raises(RuntimeError) as caught:
        emit_custom_event({"type": "progress"}, writer=writer)
    assert caught.value is error
    assert called == []


def test_callback_error_does_not_leak(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def dispatch(name: str, data: Any, *, config: Any = None) -> None:
        raise RuntimeError("callback failed")

    monkeypatch.setattr("agentkit.utils.custom_events.dispatch_custom_event", dispatch)
    seen: list[Any] = []
    with caplog.at_level(logging.DEBUG, logger="agentkit.utils.custom_events"):
        emit_custom_event({"type": "progress"}, writer=seen.append)
    assert seen == [{"type": "progress"}]
    assert any(record.levelno == logging.DEBUG and "progress" in record.message for record in caplog.records)


def test_dispatch_outside_a_run_keeps_the_writer_output(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Any] = []
    with caplog.at_level(logging.DEBUG, logger="agentkit.utils.custom_events"):
        emit_custom_event({"type": "progress"}, writer=seen.append)
    assert seen == [{"type": "progress"}]
    assert any("progress" in record.message for record in caplog.records)


def test_graph_interrupt_is_reraised_as_the_same_object(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupt = GraphInterrupt()

    def dispatch(name: str, data: Any, *, config: Any = None) -> None:
        raise interrupt

    monkeypatch.setattr("agentkit.utils.custom_events.dispatch_custom_event", dispatch)
    with pytest.raises(GraphInterrupt) as caught:
        emit_custom_event({"type": "progress"}, writer=lambda _payload: None)
    assert caught.value is interrupt


def test_async_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dispatch(name: str, data: Any, *, config: Any = None) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("agentkit.utils.custom_events.adispatch_custom_event", dispatch)
    seen: list[Any] = []

    async def send() -> None:
        await aemit_custom_event({"type": "progress"}, writer=seen.append)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(send())
    assert seen == [{"type": "progress"}]


def test_sync_node_delivers_the_payload_once_on_each_path() -> None:
    events = _custom_events(_compile(_sync_node), PAYLOAD, async_node=False)
    assert len(events) == 1
    assert events[0]["name"] == PAYLOAD["type"]
    assert events[0]["data"] == PAYLOAD


def test_async_node_delivers_the_payload_once_on_each_path() -> None:
    events = _custom_events(_compile(_async_node), ASYNC_PAYLOAD, async_node=True)
    assert len(events) == 1
    assert events[0]["name"] == ASYNC_PAYLOAD["type"]
    assert events[0]["data"] == ASYNC_PAYLOAD
