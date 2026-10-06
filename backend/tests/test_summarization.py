"""Compaction keeps tool pairs intact and stores the summary outside messages."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
    SummaryGenerationError,
    create_summarization_middleware,
)
from agentkit.config import AppConfig
from agentkit.config.model_config import ModelConfig
from agentkit.config.summarization_config import ContextSize, SummarizationConfig
from langgraph.graph.message import REMOVE_ALL_MESSAGES


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    def __init__(self, text: str = "SUMMARY", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.tags = None
        self.calls = 0
        self.prompts: list[str] = []
        self._llm_type = "fake"

    def with_config(self, **kwargs):
        clone = FakeModel(self.text, fail=self.fail)
        clone.tags = kwargs.get("tags")
        return clone

    def with_retry(self, **kwargs):
        return self

    def invoke(self, prompt, config=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("summary down")
        return _Response(self.text)

    async def ainvoke(self, prompt, config=None):
        return self.invoke(prompt, config)

    def _get_ls_params(self):
        return {}


def _counter(messages) -> int:
    return len(list(messages))


def _middleware(model: FakeModel | None = None, **kwargs) -> DeerFlowSummarizationMiddleware:
    return DeerFlowSummarizationMiddleware(
        model or FakeModel(),
        trigger=kwargs.pop("trigger", ("messages", 3)),
        keep=kwargs.pop("keep", ("messages", 2)),
        token_counter=_counter,
        trim_tokens_to_summarize=kwargs.pop("trim_tokens_to_summarize", 4000),
        **kwargs,
    )


def _tool_pair() -> tuple[AIMessage, ToolMessage]:
    ai = AIMessage(
        content="",
        id="call-ai",
        tool_calls=[{"id": "c1", "name": "bash", "args": {}}],
    )
    tool = ToolMessage(content="ok", tool_call_id="c1", id="call-tool")
    return ai, tool


def test_tool_pair_stays_on_one_side_of_the_cutoff() -> None:
    ai, tool = _tool_pair()
    human = HumanMessage(content="question", id="u2")
    analysis = AIMessage(content="long analysis", id="analysis")
    tail = AIMessage(content="tail", id="tail")
    messages = [human, analysis, ai, tool, tail]
    prepared = _middleware(keep=("messages", 2))._prepare_compaction({"messages": messages}, force=True)
    assert prepared is not None
    to_summarize, preserved, _previous, _tokens = prepared
    sides = []
    for message in (ai, tool):
        sides.append("summarize" if message in to_summarize else "preserve")
    assert sides[0] == sides[1]
    assert human in preserved
    assert analysis in to_summarize


def test_latest_user_request_is_kept_by_id_without_blocking_a_long_first_turn() -> None:
    human = HumanMessage(content="analyze this", id="u1")
    analysis = [AIMessage(content=f"step {index}", id=f"a{index}") for index in range(6)]
    prepared = _middleware(keep=("messages", 2))._prepare_compaction(
        {"messages": [human, *analysis]},
        force=True,
    )
    assert prepared is not None
    to_summarize, preserved, _previous, _tokens = prepared
    assert human in preserved
    assert any(message.id == "a0" for message in to_summarize)
    result = _middleware().before_model({"messages": [human, *analysis]}, object())
    assert result is not None
    assert result["summary_text"] == "SUMMARY"
    assert all(getattr(message, "content", None) != "SUMMARY" for message in result["messages"])
    assert result["messages"][0].id == REMOVE_ALL_MESSAGES
    assert human in result["messages"]


def test_summary_text_counts_toward_the_trigger() -> None:
    messages = [
        AIMessage(content="old", id="old"),
        HumanMessage(content="one", id="u1"),
        AIMessage(content="two", id="a1"),
    ]
    middleware = _middleware(trigger=("messages", 4), keep=("messages", 1))
    assert middleware._prepare_compaction({"messages": messages}, force=False) is None
    prepared = middleware._prepare_compaction(
        {"messages": messages, "summary_text": "earlier summary"},
        force=False,
    )
    assert prepared is not None
    to_summarize, _preserved, _previous, _tokens = prepared
    assert any(message.id == "old" for message in to_summarize)


def test_previous_user_suffix_can_be_summarized() -> None:
    companion = HumanMessage(content="old wrapped question", id="u1__user")
    current = HumanMessage(content="current question", id="u2")
    filler = [AIMessage(content=f"n{index}", id=f"n{index}") for index in range(4)]
    prepared = _middleware(keep=("messages", 2))._prepare_compaction(
        {"messages": [companion, current, *filler]},
        force=True,
    )
    assert prepared is not None
    to_summarize, preserved, _previous, _tokens = prepared
    assert companion in to_summarize
    assert current in preserved


def test_canned_summary_does_not_call_the_fallback_model() -> None:
    primary = FakeModel()
    fallback = FakeModel(fail=True)
    middleware = _middleware(primary, fallback_model=fallback)
    middleware._trim_messages_for_summary = lambda messages: []

    async def run() -> str | None:
        return await middleware._asummarize_with([HumanMessage(content="hi", id="u1")], previous_summary=None)

    assert asyncio.run(run()) == "Previous conversation was too long to summarize."
    assert fallback.calls == 0


def test_fallback_model_summarizes_the_same_conversation() -> None:
    primary = FakeModel(fail=True)
    fallback = FakeModel(text="from fallback")
    middleware = _middleware(primary, fallback_model=fallback)
    human = HumanMessage(content="question", id="u1")
    analysis = [AIMessage(content=f"step {index}", id=f"a{index}") for index in range(6)]
    result = middleware.before_model({"messages": [human, *analysis]}, object())
    assert result is not None
    assert result["summary_text"] == "from fallback"
    prompt = middleware._fallback_model.prompts[0]
    assert prompt != "fallback"
    assert "step 0" in prompt
    assert "step 5" not in prompt


def test_failed_summary_is_swallowed_on_the_auto_path_and_raised_when_asked() -> None:
    middleware = _middleware(FakeModel(fail=True))
    human = HumanMessage(content="question", id="u1")
    analysis = [AIMessage(content=f"step {index}", id=f"a{index}") for index in range(6)]
    assert middleware.before_model({"messages": [human, *analysis]}, object()) is None

    async def run() -> None:
        await middleware.acompact_state({"messages": [human, *analysis]}, object(), force=True, raise_on_failure=True)

    try:
        asyncio.run(run())
    except SummaryGenerationError as exc:
        assert "summary generation failed" in str(exc)
    else:
        raise AssertionError("manual compaction should raise")


def test_trim_none_does_not_drop_messages() -> None:
    middleware = _middleware(trim_tokens_to_summarize=None)
    messages = [HumanMessage(content="x" * 50, id=str(index)) for index in range(30)]
    assert middleware.trim_tokens_to_summarize is None
    assert middleware._trim_messages_for_summary(messages) == messages


def test_factory_passes_null_trim_and_skips_a_disabled_config(monkeypatch) -> None:
    created: list = []

    def create_chat_model(**kwargs):
        model = FakeModel()
        created.append(kwargs)
        return model

    monkeypatch.setattr(
        "agentkit.agents.middlewares.summarization_middleware.create_chat_model",
        create_chat_model,
    )
    assert create_summarization_middleware(app_config=AppConfig()) is None
    assert created == []
    app_config = AppConfig(
        models=[ModelConfig(name="lead", use="langchain_openai:ChatOpenAI")],
        summarization=SummarizationConfig(
            enabled=True,
            model_name=None,
            trigger=ContextSize(type="messages", value=10),
            trim_tokens_to_summarize=None,
        ),
    )
    middleware = create_summarization_middleware(app_config=app_config, run_model_name="lead")
    assert middleware is not None
    assert middleware.trim_tokens_to_summarize is None
    assert middleware._summary_model.tags == ["middleware:summarize", "nostream"]
    assert middleware.model.tags is None


def test_fraction_trigger_is_dropped_when_the_model_has_no_context_window(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.summarization_middleware.create_chat_model",
        lambda **kwargs: FakeModel(),
    )
    app_config = AppConfig(
        models=[ModelConfig(name="lead", use="langchain_openai:ChatOpenAI")],
        summarization=SummarizationConfig(
            enabled=True,
            trigger=[
                ContextSize(type="fraction", value=0.8),
                ContextSize(type="messages", value=10),
            ],
        ),
    )
    middleware = create_summarization_middleware(app_config=app_config, run_model_name="lead")
    assert middleware is not None
    assert middleware._trigger_clauses == [{"messages": 10}]
    assert any("fraction" in record.message for record in caplog.records)
