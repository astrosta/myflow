from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from agentkit.subagents.config import SubagentConfig
from agentkit.subagents.executor import SubagentExecutor, _RunState
from agentkit.subagents.results import SubagentResult, SubagentStatus
from agentkit.tools.builtin.task_tool import SubagentDispatch


def _dispatch() -> SubagentDispatch:
    holder = SubagentResult(
        task_id="exec-1",
        trace_id="a" * 32,
        status=SubagentStatus.PENDING,
        external_task_id="call-1",
    )
    return SubagentDispatch(
        prompt="do it",
        config=SubagentConfig(name="bash", description="bash helper"),
        holder=holder,
        model_name="deepseek-reasoner",
        label="bash",
        run_id="run-1",
        thread_id="thread-1",
        user_id="user-1",
        parent_model="deepseek-reasoner",
        uploaded_files=None,
        tools=[],
        allowed_subagents=None,
        trace_parent_span_id="b" * 16,
    )


def test_subagent_stream_attaches_its_own_handler_and_finishes_the_span(monkeypatch) -> None:
    captured: dict = {}

    class _Agent:
        async def astream(self, state, *, config=None, context=None, stream_mode=None):
            captured["config"] = config
            yield {"messages": [AIMessage(content="done")]}

    handler = object()
    fake_trace = SimpleNamespace(handler=handler, finish=Mock())
    monkeypatch.setattr(
        "agentkit.subagents.executor.start_subagent_trace",
        lambda **kwargs: fake_trace,
    )

    executor = SubagentExecutor(app_config=None, lazy_init=False)
    monkeypatch.setattr(
        executor,
        "_build_agent",
        lambda dispatch, middlewares: _Agent(),
    )

    dispatch = _dispatch()
    asyncio.run(executor._astream(_RunState(), dispatch))

    assert handler in captured["config"]["callbacks"]
    assert dispatch.holder.status is SubagentStatus.COMPLETED
    fake_trace.finish.assert_called_once_with(status="success")


def test_subagent_stream_marks_the_span_error_and_reraises(monkeypatch) -> None:
    class _Agent:
        async def astream(self, state, *, config=None, context=None, stream_mode=None):
            if False:
                yield None
            raise RuntimeError("model down")

    fake_trace = SimpleNamespace(handler=None, finish=Mock())
    monkeypatch.setattr(
        "agentkit.subagents.executor.start_subagent_trace",
        lambda **kwargs: fake_trace,
    )

    executor = SubagentExecutor(app_config=None, lazy_init=False)
    monkeypatch.setattr(
        executor,
        "_build_agent",
        lambda dispatch, middlewares: _Agent(),
    )

    try:
        asyncio.run(executor._astream(_RunState(), _dispatch()))
    except RuntimeError as exc:
        assert "model down" in str(exc)
    else:
        raise AssertionError("the subagent error must propagate")

    fake_trace.finish.assert_called_once()
    assert fake_trace.finish.call_args.kwargs["status"] == "error"
