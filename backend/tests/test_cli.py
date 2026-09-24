import io

import pytest

from agentkit.cli import main, run_repl
from agentkit.client import StreamEvent


class _Client:
    def __init__(self, events: list[StreamEvent] | None = None) -> None:
        self.events = events or [
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hel"}),
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "lo"}),
            StreamEvent(type="end", data={"usage": {}}),
        ]
        self.calls: list[dict] = []
        self.reset_agent_calls = 0

    def stream(self, message: str, *, thread_id: str, **overrides):
        self.calls.append({"message": message, "thread_id": thread_id, "overrides": overrides})
        return iter(self.events)

    def reset_agent(self) -> None:
        self.reset_agent_calls += 1


def _input(lines: list[str]):
    pending = iter(lines)

    def read(_prompt: str = "") -> str:
        try:
            return next(pending)
        except StopIteration:
            raise EOFError

    return read


def test_user_line_streams_and_prints_ai_deltas() -> None:
    client = _Client()
    output = io.StringIO()
    run_repl(client, thread_id="t1", input_fn=_input(["hello"]), output=output)
    assert client.calls == [{"message": "hello", "thread_id": "t1", "overrides": {}}]
    assert "hello" in output.getvalue()
    assert client.reset_agent_calls == 0


def test_thread_id_stays_until_reset() -> None:
    client = _Client()
    run_repl(client, thread_id="t1", input_fn=_input(["one", "two", "/reset", "three"]), output=io.StringIO())
    assert [call["thread_id"] for call in client.calls] == ["t1", "t1", client.calls[2]["thread_id"]]
    assert client.calls[2]["thread_id"] != "t1"
    assert client.calls[2]["message"] == "three"
    assert client.reset_agent_calls == 0


def test_model_command_sets_model_name_without_resetting_the_graph() -> None:
    client = _Client()
    output = io.StringIO()
    run_repl(
        client,
        thread_id="t1",
        input_fn=_input(["/model deepseek-reasoner", "hello", "again"]),
        output=output,
    )
    assert client.reset_agent_calls == 0
    assert len(client.calls) == 2
    assert client.calls[0]["overrides"] == {"model_name": "deepseek-reasoner"}
    assert client.calls[1]["overrides"] == {"model_name": "deepseek-reasoner"}
    assert client.calls[0]["thread_id"] == "t1"


def test_model_without_a_name_does_not_call_stream() -> None:
    client = _Client()
    output = io.StringIO()
    run_repl(client, thread_id="t1", input_fn=_input(["/model", "hi"]), output=output)
    assert len(client.calls) == 1
    assert client.calls[0]["overrides"] == {}
    assert "usage" in output.getvalue()


def test_slash_commands_are_not_sent_to_the_model() -> None:
    client = _Client()
    run_repl(client, thread_id="t1", input_fn=_input(["/nope", "  "]), output=io.StringIO())
    assert client.calls == []


def test_main_pins_the_session_checkpointer_and_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    saver = object()
    created: list[object] = []
    seen: dict[str, str] = {}

    def checkpointer_context():
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield saver

        return _ctx()

    class _Pinned:
        def __init__(self, *, checkpointer=None, app_config=None) -> None:
            created.append(checkpointer)

    def run(client, *, thread_id: str, input_fn=input, output=None) -> None:
        seen["thread_id"] = thread_id
        assert client is not None

    monkeypatch.setattr("agentkit.cli.checkpointer_context", checkpointer_context)
    monkeypatch.setattr("agentkit.cli.AgentClient", _Pinned)
    monkeypatch.setattr("agentkit.cli.run_repl", run)
    main(["--thread-id", "kept"])
    assert created == [saver]
    assert seen["thread_id"] == "kept"
