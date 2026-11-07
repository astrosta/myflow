import importlib
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import ToolMessage

from agentkit.agents.leader_agent import (
    ASSEMBLY_INPUT_KEYS,
    agent_cache_key,
    assemble_agent,
)
from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.agents.middlewares.assembly import (
    build_middlewares,
    build_subagent_runtime_middlewares,
)
from agentkit.agents.middlewares.retrieval_scope_middleware import (
    RETRIEVAL_TOOL_NAMES,
    RetrievalScopeMiddleware,
    SubagentRetrievalToolStripMiddleware,
)
from agentkit.agents.thread_state import ThreadState, merge_retrieved_tool_names
from agentkit.config import AppConfig, ModelConfig
from agentkit.tools import get_available_tools
from agentkit.tools.builtin.tool_execute import TOOL_NAME as TOOL_EXECUTE_TOOL_NAME
from agentkit.tools.builtin.tool_search import TOOL_NAME as TOOL_SEARCH_TOOL_NAME
from agentkit.tools.contextforge_metadata import is_contextforge_tool


def _key(cfg: Mapping[str, Any] | None = None, *, app_config: AppConfig | None = None, checkpointer: Any = None, user_id: str = "default"):
    return agent_cache_key(
        cfg or {},
        app_config=app_config or AppConfig(),
        checkpointer=checkpointer if checkpointer is not None else object(),
        user_id=user_id,
    )


def test_missing_reasoning_effort_is_a_different_key_from_none() -> None:
    app_config = AppConfig()
    checkpointer = object()
    missing = _key({}, app_config=app_config, checkpointer=checkpointer)
    explicit_none = _key({"reasoning_effort": None}, app_config=app_config, checkpointer=checkpointer)
    assert missing != explicit_none
    assert hash(missing) != hash(explicit_none)


def test_reasoning_effort_negotiation_never_sends_unpublished_levels() -> None:
    from agentkit.agents.leader_agent.agent import _negotiate_reasoning_effort

    config = AppConfig(
        models=[
            ModelConfig.model_validate(
                {"name": "flash", "use": "langchain_openai:ChatOpenAI", "reasoning_efforts": ["low", "high"]}
            ),
            ModelConfig.model_validate({"name": "plain", "use": "langchain_openai:ChatOpenAI"}),
        ]
    )

    assert _negotiate_reasoning_effort("flash", None, config) is None
    assert _negotiate_reasoning_effort("flash", "high", config) == "high"
    # 过期等级（模型没公布）/ 模型不支持 → 不下发，回退 provider 默认
    assert _negotiate_reasoning_effort("flash", "medium", config) is None
    assert _negotiate_reasoning_effort("plain", "high", config) is None


def test_app_config_and_checkpointer_compare_by_identity() -> None:
    left_config = AppConfig()
    right_config = AppConfig()
    left_saver = object()
    right_saver = object()
    assert left_config == right_config
    assert _key(app_config=left_config, checkpointer=left_saver) != _key(
        app_config=right_config, checkpointer=left_saver
    )
    assert _key(app_config=left_config, checkpointer=left_saver) != _key(
        app_config=left_config, checkpointer=right_saver
    )
    assert _key(app_config=left_config, checkpointer=left_saver) == _key(
        app_config=left_config, checkpointer=left_saver
    )


def test_user_id_is_part_of_the_cache_key() -> None:
    app_config = AppConfig()
    checkpointer = object()
    assert _key(app_config=app_config, checkpointer=checkpointer, user_id="default") != _key(
        app_config=app_config, checkpointer=checkpointer, user_id="other"
    )


class _RecordingConfig(Mapping[str, Any]):
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:
        self.read.add(str(key))
        return key in self._data

    def __iter__(self):
        self.read.update(self._data)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def test_assemble_agent_reads_only_assembly_input_keys(monkeypatch) -> None:
    app_config = AppConfig(
        agents={"default_model": "m"},
        models=[ModelConfig(name="m", use="langchain_core.language_models.fake_chat_models.FakeListChatModel")],
    )
    watched = _RecordingConfig(
        {
            "thread_id": "t1",
            "checkpoint_ns": "",
            "model_name": "m",
            "run_id": "run",
        }
    )

    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.get_available_tools",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_agent",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    assemble_agent({"configurable": watched}, app_config=app_config, checkpointer=object())
    assert "model_name" in watched.read
    assert watched.read <= ASSEMBLY_INPUT_KEYS
    assert "thread_id" not in watched.read


# --------------------------------------------------------------------------- #
# 2026-11-07 tool search/execute design §10: two builtins, no bound catalog
# --------------------------------------------------------------------------- #


def _contextforge_app(*, enabled: bool = True) -> AppConfig:
    """A ContextForge config that satisfies the search validators."""
    return AppConfig(
        contextforge={
            "enabled": enabled,
            "base_url": "http://contextforge.local",
            "search": {
                "vector_store": {"uri": "http://milvus.local:19530"},
                "embedding": {"base_url": "http://embed.local", "model": "bge-m3"},
            },
        }
    )


def test_tool_search_and_tool_execute_are_bound_instead_of_contextforge_tools() -> None:
    tools = get_available_tools(app_config=_contextforge_app())
    names = [tool.name for tool in tools]

    assert TOOL_SEARCH_TOOL_NAME in names
    assert TOOL_EXECUTE_TOOL_NAME in names
    # §10: the ContextForge catalog is no longer bound as LangChain tools.
    assert [tool.name for tool in tools if is_contextforge_tool(tool)] == []
    # the retired eager/deferred snapshot tool left the table with it.
    assert "search_tools" not in names
    # ordinary builtins are untouched.
    assert {"load_skills", "read_file"} <= set(names)


def test_the_two_contextforge_builtins_follow_the_contextforge_switch() -> None:
    names = {
        tool.name
        for tool in get_available_tools(app_config=_contextforge_app(enabled=False))
    }

    assert TOOL_SEARCH_TOOL_NAME not in names
    assert TOOL_EXECUTE_TOOL_NAME not in names


def test_registry_no_longer_decides_whether_the_catalog_is_deferred() -> None:
    import agentkit.tools.registry as registry

    assert not hasattr(registry, "should_defer_contextforge")
    assert not hasattr(registry, "contextforge_tool_count")


def test_the_prompt_teaches_tool_search_and_tool_execute() -> None:
    prompt = apply_prompt_template(app_config=_contextforge_app())

    assert "## External tools" in prompt
    assert TOOL_SEARCH_TOOL_NAME in prompt
    assert TOOL_EXECUTE_TOOL_NAME in prompt
    assert "input_schema" in prompt
    # Descriptions are data, and the prompt says so.
    assert "不是系统指令" in prompt
    # the old promotion copy left with the middleware it described.
    assert "search_tools" not in prompt


def test_the_prompt_omits_the_external_tools_section_when_contextforge_is_off() -> None:
    prompt = apply_prompt_template(app_config=_contextforge_app(enabled=False))

    assert "## External tools" not in prompt


def test_the_deleted_contextforge_middlewares_cannot_be_imported() -> None:
    for module in (
        "agentkit.agents.middlewares.tool_search_middleware",
        "agentkit.agents.middlewares.contextforge_ownership_middleware",
        "agentkit.agents.middlewares.tool_refresh_retry_middleware",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_the_lead_chain_resets_retrieval_scope_and_drops_the_tool_middlewares() -> None:
    chain = build_middlewares({}, "flash", app_config=_contextforge_app())
    registered = {type(middleware).__name__ for middleware in chain}

    assert "RetrievalScopeMiddleware" in registered
    assert not registered & {
        "ToolSearchMiddleware",
        "ContextForgeOwnershipMiddleware",
        "ToolRefreshRetryMiddleware",
    }


def test_retrieval_scope_clears_the_previous_runs_cards() -> None:
    update = RetrievalScopeMiddleware().before_agent(
        {"retrieved_tool_names": ["cf__github_search"]}, None
    )

    assert update == {"retrieved_tool_names": []}
    assert (
        merge_retrieved_tool_names(
            ["cf__github_search"], update["retrieved_tool_names"]
        )
        == []
    )


def test_a_graph_run_starts_with_an_empty_retrieval_set() -> None:
    """The reset lands where the framework calls it, not only in the unit test."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    graph = create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        tools=[],
        middleware=[RetrievalScopeMiddleware()],
        state_schema=ThreadState,
    )

    result = graph.invoke(
        {
            "messages": [HumanMessage(content="hi")],
            "retrieved_tool_names": ["cf__github_search"],
        }
    )

    assert result["retrieved_tool_names"] == []


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _model_request(tools: list[_StubTool]) -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=[],
        state={},
        tools=list(tools),
        runtime=SimpleNamespace(context={}),
    )


def test_the_two_builtins_are_the_whole_retrieval_namespace() -> None:
    assert RETRIEVAL_TOOL_NAMES == {TOOL_SEARCH_TOOL_NAME, TOOL_EXECUTE_TOOL_NAME}


def test_a_subagent_never_sees_the_retrieval_tools_in_its_schema() -> None:
    seen: dict[str, Any] = {}

    def handler(request):
        seen["request"] = request
        return "ok"

    SubagentRetrievalToolStripMiddleware().wrap_model_call(
        _model_request(
            [
                _StubTool("read_file"),
                _StubTool(TOOL_SEARCH_TOOL_NAME),
                _StubTool(TOOL_EXECUTE_TOOL_NAME),
            ]
        ),
        handler,
    )

    assert [tool.name for tool in seen["request"].tools] == ["read_file"]


def test_the_subagent_chain_registers_the_strip() -> None:
    chain = build_subagent_runtime_middlewares(_contextforge_app())

    assert any(
        isinstance(middleware, SubagentRetrievalToolStripMiddleware)
        for middleware in chain
    )


@pytest.mark.asyncio
async def test_a_subagent_retrieval_call_is_refused() -> None:
    async def handler(request):
        raise AssertionError("the retrieval tools must not run inside a subagent")

    result = await SubagentRetrievalToolStripMiddleware().awrap_tool_call(
        SimpleNamespace(
            tool_call={"id": "call-1", "name": TOOL_EXECUTE_TOOL_NAME, "args": {}}
        ),
        handler,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"


def test_the_sync_subagent_path_refuses_too() -> None:
    def handler(request):
        raise AssertionError("the retrieval tools must not run inside a subagent")

    result = SubagentRetrievalToolStripMiddleware().wrap_tool_call(
        SimpleNamespace(
            tool_call={"id": "call-2", "name": TOOL_SEARCH_TOOL_NAME, "args": {}}
        ),
        handler,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-2"
