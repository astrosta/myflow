"""Manual compact shares the thread lock and writes one new checkpoint."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agentkit.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
    SummaryGenerationError,
    compact_http_error,
    compact_with_reservation,
)
from agentkit.agents.thread_state import get_thread_state_schema
from agentkit.runtime.runs.manager import RunManager
from agentkit.persistence.run.memory import MemoryRunStore
from agentkit.runtime.runs.worker import build_state_mutation_graph


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Model:
    def __init__(self, text: str = "compressed") -> None:
        self.text = text
        self.tags = None
        self._llm_type = "fake"

    def with_config(self, **kwargs):
        clone = _Model(self.text)
        clone.tags = kwargs.get("tags")
        return clone

    def with_retry(self, **kwargs):
        return self

    def invoke(self, prompt, config=None):
        return _Response(self.text)

    async def ainvoke(self, prompt, config=None):
        return self.invoke(prompt, config)

    def _get_ls_params(self):
        return {}


def _middleware() -> DeerFlowSummarizationMiddleware:
    return DeerFlowSummarizationMiddleware(
        _Model(),
        trigger=("messages", 3),
        keep=("messages", 2),
        token_counter=lambda messages: len(list(messages)),
    )


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_active_run_rejects_compact_with_409() -> None:
    async def scenario() -> None:
        manager = RunManager(MemoryRunStore())
        await manager.create_or_reject("thread-1", None, user_id="alice")
        try:
            await compact_with_reservation(
                manager,
                InMemorySaver(),
                "thread-1",
                user_id="alice",
                middleware=_middleware(),
                force=True,
            )
        except Exception as exc:
            status, error = compact_http_error(exc)
            assert status == 409
            assert error == "thread_busy"
        else:
            raise AssertionError("an active run must reject compact")

    _run(scenario)


def test_idle_compact_writes_a_new_checkpoint() -> None:
    async def scenario() -> None:
        saver = InMemorySaver()
        graph = build_state_mutation_graph("seed", checkpointer=saver, state_schema=get_thread_state_schema())
        messages = [HumanMessage(content="question", id="u1")]
        messages.extend(AIMessage(content=f"step {index}", id=f"a{index}") for index in range(6))
        await graph.ainvoke({"messages": messages}, {"configurable": {"thread_id": "thread-1"}})
        before = await saver.aget_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})
        outcome = await compact_with_reservation(
            RunManager(MemoryRunStore()),
            saver,
            "thread-1",
            user_id="alice",
            middleware=_middleware(),
            force=True,
        )
        assert outcome.compacted is True
        assert outcome.summary_text == "compressed"
        after = await saver.aget_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})
        assert after.config["configurable"]["checkpoint_id"] != before.config["configurable"]["checkpoint_id"]
        assert after.checkpoint["channel_values"]["summary_text"] == "compressed"
        assert all(getattr(message, "content", None) != "compressed" for message in after.checkpoint["channel_values"]["messages"])

    _run(scenario)


def test_summary_failure_maps_to_500() -> None:
    status, error = compact_http_error(SummaryGenerationError("summary generation failed"))
    assert status == 500
    assert error == "summarization_failed"
