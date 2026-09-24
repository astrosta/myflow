from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Iterator
from typing import Any, TextIO

from agentkit.client import AgentClient, StreamEvent
from agentkit.runtime.checkpointer import checkpointer_context

InputFn = Callable[[str], str]


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    thread_id = args.thread_id or uuid.uuid4().hex
    with checkpointer_context() as checkpointer:
        client = AgentClient(checkpointer=checkpointer)
        run_repl(client, thread_id=thread_id)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentkit")
    parser.add_argument("--thread-id", default=None, help="Continue this thread")
    return parser.parse_args(argv)


def run_repl(
    client: Any,
    *,
    thread_id: str,
    input_fn: InputFn = input,
    output: TextIO | None = None,
) -> None:
    sink = output if output is not None else sys.stdout
    model_name: str | None = None
    sink.write(f"thread {thread_id}\n")
    sink.flush()
    while True:
        try:
            line = input_fn("> ")
        except EOFError:
            return
        command = line.strip()
        if not command:
            continue
        if command.startswith("/"):
            thread_id, model_name = _handle_command(command, thread_id, model_name, sink)
            continue
        overrides: dict[str, Any] = {}
        if model_name is not None:
            overrides["model_name"] = model_name
        # stream() is a sync generator and shares this context. Do not bind a
        # ContextVar around the iteration; a binding held across yield leaks
        # to the caller and can crash when the generator is finalized.
        for event in _step(client.stream(command, thread_id=thread_id, **overrides)):
            _render(event, sink)


def _step(events: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
    """Pull one event per step so the caller's context is not held across yield."""
    iterator = iter(events)
    while True:
        try:
            event = next(iterator)
        except StopIteration:
            return
        yield event


def _handle_command(
    command: str,
    thread_id: str,
    model_name: str | None,
    output: TextIO,
) -> tuple[str, str | None]:
    if command == "/reset" or command.startswith("/reset "):
        thread_id = uuid.uuid4().hex
        output.write(f"thread {thread_id}\n")
        output.flush()
        return thread_id, model_name
    if command == "/model" or command.startswith("/model "):
        name = command.removeprefix("/model").strip()
        if not name:
            output.write("usage: /model <name>\n")
            output.flush()
            return thread_id, model_name
        output.write(f"model {name}\n")
        output.flush()
        return thread_id, name
    output.write(f"unknown command: {command.split()[0]}\n")
    output.flush()
    return thread_id, model_name


def _render(event: StreamEvent, output: TextIO) -> None:
    if event.type == "messages-tuple" and event.data.get("type") == "ai":
        text = event.data.get("content") or ""
        if text:
            output.write(text)
            output.flush()
        return
    if event.type == "messages-tuple" and event.data.get("type") == "tool":
        name = event.data.get("name") or "tool"
        output.write(f"\n[{name}] {event.data.get('content')}\n")
        output.flush()
        return
    if event.type == "custom":
        output.write(f"\n[{event.data.get('type')}]\n")
        output.flush()
        return
    if event.type == "end":
        output.write("\n")
        output.flush()


if __name__ == "__main__":
    main()
