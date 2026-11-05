from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.callbacks import CallbackManager

from agentkit.config.observability_config import LangfuseConfig
from agentkit.observability.runtime import (
    ObservabilityRuntime,
    attach_callback_handler,
    pseudonymize_user_id,
)

TRACE_ID = "a" * 32
ROOT_ID = "b" * 16
SUB_ID = "c" * 16


class _Span:
    def __init__(self, span_id: str = ROOT_ID) -> None:
        self.id = span_id
        self.updates: list[dict] = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def end(self) -> None:
        self.ended = True


class _CM:
    def __init__(self, span=None) -> None:
        self.span = span
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self.span

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        if self.span is not None:
            self.span.end()
        return False


class _Client:
    def __init__(self) -> None:
        self.start_calls: list[dict] = []
        self.span = _Span()
        self.flushed = 0
        self.shutdowns = 0
        self.current_id = ROOT_ID

    def start_as_current_observation(self, **kwargs):
        self.start_calls.append(kwargs)
        span = _Span(str(kwargs.get("as_type", ROOT_ID)) if False else ROOT_ID)
        self.span = span
        return _CM(span)

    def get_current_observation_id(self):
        return self.current_id

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


class _Handler:
    def __init__(self, *, trace_context=None) -> None:
        self.trace_context = trace_context or {}


def _runtime(**overrides) -> tuple[ObservabilityRuntime, _Client, list[dict]]:
    values = {
        "enabled": True,
        "base_url": "https://langfuse.internal.example",
        "public_key": "pk-lf-test",
        "secret_key": "sk-lf-test",
        "user_id_salt": "a" * 32,
    }
    values.update(overrides)
    config = LangfuseConfig(**values)
    client = _Client()
    propagations: list[dict] = []

    def propagate(**kwargs):
        propagations.append(kwargs)
        return _CM(None)

    runtime = ObservabilityRuntime(
        config,
        client=client,
        handler_factory=_Handler,
        propagate_attributes=propagate,
    )
    return runtime, client, propagations


def test_run_trace_uses_the_myflow_trace_id_and_parents_the_handler() -> None:
    runtime, client, propagations = _runtime()

    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        model_name="deepseek-reasoner",
        assistant_id="lead",
        trace_id=TRACE_ID,
    )

    assert trace.trace_id == TRACE_ID
    assert trace.root_span_id == ROOT_ID
    assert client.start_calls[0]["trace_context"] == {"trace_id": TRACE_ID}
    assert client.start_calls[0]["as_type"] == "agent"
    assert trace.handler.trace_context == {
        "trace_id": TRACE_ID,
        "parent_span_id": ROOT_ID,
    }
    assert trace.handler.myflow_loop_bound is True
    assert propagations[0]["session_id"] == "thread-1"
    assert propagations[0]["trace_name"] == "myflow.run"

    trace.finish(status="success")
    assert client.span.updates[-1]["output"] == {"status": "success"}
    assert client.span.ended is True


def test_error_finish_marks_the_root_span_and_never_raises() -> None:
    runtime, client, _ = _runtime()
    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id=None,
        model_name=None,
        assistant_id=None,
        trace_id=TRACE_ID,
    )

    trace.finish(status="error", error="provider exploded")

    assert client.span.updates[-1]["level"] == "ERROR"
    assert "provider exploded" in client.span.updates[-1]["status_message"]
    assert client.span.ended is True


def test_finish_is_idempotent_and_does_not_flush_by_default() -> None:
    runtime, client, _ = _runtime()
    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id=None,
        model_name=None,
        assistant_id=None,
        trace_id=TRACE_ID,
    )

    trace.finish(status="success")
    trace.finish(status="error", error="late")

    assert client.span.ended is True
    assert client.flushed == 0


def test_flush_on_run_end_uses_the_client() -> None:
    runtime, client, _ = _runtime(flush_on_run_end=True)
    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id=None,
        model_name=None,
        assistant_id=None,
        trace_id=TRACE_ID,
    )

    asyncio.run(trace.aclose(status="success"))

    assert client.flushed == 1


def test_disabled_runtime_returns_a_noop_trace() -> None:
    runtime = ObservabilityRuntime.disabled()

    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id=None,
        model_name=None,
        assistant_id=None,
        trace_id=TRACE_ID,
    )

    assert trace.handler is None
    assert trace.root_span_id is None
    trace.finish(status="success")


def test_invalid_trace_id_skips_tracing_instead_of_failing_the_run() -> None:
    runtime, client, _ = _runtime()

    trace = runtime.start_run(
        run_id="run-1",
        thread_id="thread-1",
        user_id=None,
        model_name=None,
        assistant_id=None,
        trace_id="not-a-trace-id",
    )

    assert trace.handler is None
    assert client.start_calls == []


def test_subagent_trace_is_a_child_of_the_run_root_and_is_not_loop_bound() -> None:
    runtime, client, _ = _runtime()
    client.current_id = SUB_ID

    trace = runtime.start_subagent(
        trace_id=TRACE_ID,
        parent_span_id=ROOT_ID,
        name="bash",
        execution_id="exec-1",
        tool_call_id="call-1",
        user_id="user-1",
        thread_id="thread-1",
        model_name="deepseek-reasoner",
    )

    assert trace.root_span_id == SUB_ID
    assert client.start_calls[0]["trace_context"] == {
        "trace_id": TRACE_ID,
        "parent_span_id": ROOT_ID,
    }
    assert trace.handler.trace_context["parent_span_id"] == SUB_ID
    assert getattr(trace.handler, "myflow_loop_bound", False) is False

    trace.finish(status="success")
    assert client.span.ended is True


def test_trace_subagents_off_returns_noop() -> None:
    runtime, client, _ = _runtime(trace_subagents=False)

    trace = runtime.start_subagent(
        trace_id=TRACE_ID,
        parent_span_id=ROOT_ID,
        name="bash",
        execution_id="exec-1",
        tool_call_id="call-1",
        user_id=None,
        thread_id="thread-1",
        model_name=None,
    )

    assert trace.handler is None
    assert client.start_calls == []


def test_attach_callback_handler_preserves_existing_handlers() -> None:
    first = object()
    second = object()
    handler = object()

    assert attach_callback_handler({}, handler)["callbacks"] == [handler]
    assert attach_callback_handler({"callbacks": [first]}, handler)["callbacks"] == [first, handler]
    assert attach_callback_handler({"callbacks": first}, handler)["callbacks"] == [first, handler]

    manager = CallbackManager([first])
    attached = attach_callback_handler({"callbacks": manager}, handler)
    assert attached["callbacks"] is not manager
    assert attached["callbacks"].handlers == [first, handler]
    assert manager.handlers == [first], "parent callback manager must not be mutated"


def test_pseudonymize_user_id_is_stable_and_does_not_reveal_the_id() -> None:
    config = LangfuseConfig(
        enabled=True,
        base_url="https://langfuse.internal.example",
        public_key="pk",
        secret_key="sk",
        user_id_salt="salt-salt-salt-salt",
    )

    first = pseudonymize_user_id("alice", config)
    second = pseudonymize_user_id("alice", config)
    other = pseudonymize_user_id("bob", config)

    assert first == second
    assert first != other
    assert "alice" not in first


def test_raw_and_none_user_id_modes() -> None:
    raw = LangfuseConfig(
        enabled=True,
        base_url="https://langfuse.internal.example",
        public_key="pk",
        secret_key="sk",
        user_id_mode="raw",
    )
    none = raw.model_copy(update={"user_id_mode": "none"})

    assert pseudonymize_user_id("alice", raw) == "alice"
    assert pseudonymize_user_id("alice", none) is None
