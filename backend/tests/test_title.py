"""First-exchange titles use the original user text and never stream into the chat."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import TAG_NOSTREAM
from pydantic import ValidationError

from agentkit.agents.middlewares.title_middleware import TitleMiddleware, fallback_title_from_state
from agentkit.config import AppConfig
from agentkit.config.model_config import ModelConfig
from agentkit.config.title_config import TitleConfig
from agentkit.utils.messages import ORIGINAL_USER_CONTENT_KEY

_WRAPPED = "<current_uploads>\nfile.txt\n</current_uploads>\n用户上传了 3 个文件并询问天气"


def _state(*messages: object, title: str | None = None) -> dict:
    state: dict = {"messages": list(messages)}
    if title is not None:
        state["title"] = title
    return state


def _middleware(**overrides: object) -> TitleMiddleware:
    return TitleMiddleware(title_config=TitleConfig(**overrides))


def test_title_uses_original_text_not_the_wrapped_message() -> None:
    human = HumanMessage(
        content=_WRAPPED,
        additional_kwargs={ORIGINAL_USER_CONTENT_KEY: "今天天气怎么样"},
    )
    result = _middleware().after_model(_state(human, AIMessage(content="晴")), None)
    assert result == {"title": "今天天气怎么样"}
    assert "用户上传了" not in result["title"]


def test_attachment_only_turn_uses_the_default_title_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**kwargs: object) -> None:
        raise AssertionError(kwargs)

    monkeypatch.setattr("agentkit.agents.middlewares.title_middleware.create_chat_model", explode)
    human = HumanMessage(content=_WRAPPED, additional_kwargs={ORIGINAL_USER_CONTENT_KEY: "   "})
    middleware = _middleware(model_name="lead")

    async def run() -> dict | None:
        return await middleware.aafter_model(_state(human, AIMessage(content="收到文件")), None)

    assert middleware.after_model(_state(human, AIMessage(content="收到文件")), None) == {"title": "新对话"}
    assert asyncio.run(run()) == {"title": "新对话"}


def test_interrupted_first_turn_falls_back_without_an_assistant_message() -> None:
    state = {"messages": [HumanMessage(content="还没等到回答")]}
    assert fallback_title_from_state(state, title_config=TitleConfig()) == {"title": "还没等到回答"}


def test_existing_title_is_not_replaced() -> None:
    human = HumanMessage(content="新的问题")
    assert _middleware().after_model(_state(human, AIMessage(content="答"), title="已有标题"), None) is None


def test_async_model_call_carries_nostream_and_strips_think_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = {"tags": ["keep"], "metadata": {"thread_id": "t1"}}
    seen: dict = {}

    class _Model:
        async def ainvoke(self, prompt: str, config: dict | None = None) -> AIMessage:
            seen["prompt"] = prompt
            seen["config"] = config
            return AIMessage(content='<think>hidden</think>\n"Weather today"')

    def create_chat_model(**kwargs: object) -> _Model:
        seen["create"] = kwargs
        return _Model()

    monkeypatch.setattr("agentkit.agents.middlewares.title_middleware.create_chat_model", create_chat_model)
    monkeypatch.setattr("langgraph.config.get_config", lambda: parent)
    human = HumanMessage(
        content=_WRAPPED,
        additional_kwargs={ORIGINAL_USER_CONTENT_KEY: "今天天气怎么样"},
    )
    app_config = AppConfig()
    middleware = TitleMiddleware(app_config=app_config, title_config=TitleConfig(model_name="lead", max_words=4))

    async def run() -> dict | None:
        return await middleware.aafter_model(_state(human, AIMessage(content="晴天")), None)

    assert asyncio.run(run()) == {"title": "Weather today"}
    assert seen["create"]["name"] == "lead"
    assert seen["create"]["thinking_enabled"] is False
    assert seen["create"]["attach_tracing"] is False
    assert seen["create"]["app_config"] is app_config
    assert "今天天气怎么样" in seen["prompt"]
    assert "用户上传了" not in seen["prompt"]
    assert "最多 4 个词" in seen["prompt"]
    assert seen["config"]["run_name"] == "title_agent"
    assert seen["config"]["tags"] == ["keep", "middleware:title", TAG_NOSTREAM]
    assert parent["tags"] == ["keep"]


def test_sync_path_truncates_locally_even_when_a_model_is_configured() -> None:
    human = HumanMessage(content="abcdefghijklmnop")
    result = _middleware(model_name="lead", max_chars=12).after_model(
        _state(human, AIMessage(content="ok")),
        None,
    )
    assert result == {"title": "abcdefghi..."}


def test_model_failure_and_empty_title_use_the_same_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down:
        async def ainvoke(self, prompt: str, config: dict | None = None) -> AIMessage:
            raise RuntimeError("down")

    class _Empty:
        async def ainvoke(self, prompt: str, config: dict | None = None) -> AIMessage:
            return AIMessage(content="<THINK>secret</THINK>   ")

    monkeypatch.setattr(
        "agentkit.agents.middlewares.title_middleware.create_chat_model",
        lambda **kwargs: _Down(),
    )
    human = HumanMessage(content="abcdefghijklmnop")
    middleware = _middleware(model_name="lead", max_chars=12)

    async def failed() -> dict | None:
        return await middleware.aafter_model(_state(human, AIMessage(content="ok")), None)

    assert asyncio.run(failed()) == {"title": "abcdefghi..."}

    monkeypatch.setattr(
        "agentkit.agents.middlewares.title_middleware.create_chat_model",
        lambda **kwargs: _Empty(),
    )

    async def empty() -> dict | None:
        return await middleware.aafter_model(_state(human, AIMessage(content="ok")), None)

    assert asyncio.run(empty()) == {"title": "abcdefghi..."}


def test_parsed_title_is_cut_at_max_chars_without_an_ellipsis(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Model:
        async def ainvoke(self, prompt: str, config: dict | None = None) -> AIMessage:
            return AIMessage(content="abcdefghijklmnop")

    monkeypatch.setattr(
        "agentkit.agents.middlewares.title_middleware.create_chat_model",
        lambda **kwargs: _Model(),
    )
    result_box: dict = {}

    async def run() -> None:
        result_box["result"] = await _middleware(model_name="lead", max_chars=10).aafter_model(
            _state(HumanMessage(content="hi"), AIMessage(content="ok")),
            None,
        )

    asyncio.run(run())
    assert result_box["result"] == {"title": "abcdefghij"}


def test_dynamic_context_reminder_is_not_the_user_message() -> None:
    reminder = HumanMessage(content="日期提醒", additional_kwargs={"dynamic_context_reminder": True})
    human = HumanMessage(content="真正的问题")
    result = _middleware().after_model(_state(reminder, human, AIMessage(content="答")), None)
    assert result == {"title": "真正的问题"}


def test_second_user_turn_does_not_generate_another_title() -> None:
    messages = [
        HumanMessage(content="第一问"),
        AIMessage(content="第一答"),
        HumanMessage(content="第二问"),
        AIMessage(content="第二答"),
    ]
    assert _middleware().after_model({"messages": messages}, None) is None


def test_disabled_config_returns_none() -> None:
    assert _middleware(enabled=False).after_model(
        _state(HumanMessage(content="hi"), AIMessage(content="ok")),
        None,
    ) is None


def test_explicit_title_config_wins_over_app_config() -> None:
    app_config = AppConfig(title=TitleConfig(enabled=False))
    middleware = TitleMiddleware(app_config=app_config, title_config=TitleConfig(enabled=True))
    result = middleware.after_model(_state(HumanMessage(content="你好"), AIMessage(content="在")), None)
    assert result == {"title": "你好"}


def test_prompt_truncates_each_side_to_500_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class _Model:
        async def ainvoke(self, prompt: str, config: dict | None = None) -> AIMessage:
            seen["prompt"] = prompt
            return AIMessage(content="短标题")

    monkeypatch.setattr(
        "agentkit.agents.middlewares.title_middleware.create_chat_model",
        lambda **kwargs: _Model(),
    )
    human = HumanMessage(content="u" * 600)
    assistant = AIMessage(content="a" * 600)

    async def run() -> None:
        await _middleware(model_name="lead").aafter_model(_state(human, assistant), None)

    asyncio.run(run())
    assert "u" * 500 in seen["prompt"]
    assert "u" * 501 not in seen["prompt"]
    assert "a" * 500 in seen["prompt"]
    assert "a" * 501 not in seen["prompt"]


def test_interrupted_run_persists_the_local_title() -> None:
    async def scenario() -> None:
        from langgraph.checkpoint.memory import InMemorySaver

        from agentkit.agents.thread_state import get_thread_state_schema
        from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
        from agentkit.runtime.context import RunContext
        from agentkit.runtime.runs.worker import _fallback_thread_title, build_state_mutation_graph

        saver = InMemorySaver()
        store = MemoryThreadMetaStore()
        await store.create("t1", user_id="alice")
        graph = build_state_mutation_graph("seed", checkpointer=saver, state_schema=get_thread_state_schema())
        await graph.ainvoke(
            {"messages": [HumanMessage(content="还没等到回答", id="m1")]},
            {"configurable": {"thread_id": "t1"}},
        )
        ctx = RunContext(
            checkpointer=saver,
            thread_store=store,
            run_store=object(),
            app_config=AppConfig(),
            graph_input={"messages": [HumanMessage(content="输入里的另一句")]},
        )
        record = type("Record", (), {"thread_id": "t1", "run_id": "run-1", "user_id": "alice"})()

        class _Manager:
            def __init__(self, running: bool) -> None:
                self.running = running

            async def later_run_is_running(self, thread_id: str, run_id: str) -> bool:
                return self.running

        await _fallback_thread_title(_Manager(True), record, ctx)
        assert (await store.get("t1", user_id="alice"))["display_name"] is None

        await _fallback_thread_title(_Manager(False), record, ctx)
        assert (await store.get("t1", user_id="alice"))["display_name"] == "还没等到回答"
        head = await saver.aget_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
        assert head.checkpoint["channel_values"]["title"] == "还没等到回答"

    asyncio.run(scenario())


def test_title_model_must_be_a_configured_model_name() -> None:
    with pytest.raises(ValidationError, match="title.model_name"):
        AppConfig(title=TitleConfig(model_name="missing"))
    AppConfig(
        models=[ModelConfig(name="lead", use="langchain_openai:ChatOpenAI")],
        title=TitleConfig(model_name="lead"),
    )
