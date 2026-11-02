"""Retrieval subagents: registration, tool provisioning, prompts, and loop safety."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agentkit.agents.leader_agent import prompt as prompt_module
from agentkit.agents.memory.openviking.client import OpenVikingClientFactory
from agentkit.config import AppConfig
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
from agentkit.knowledge.rerank import OpenAICompatibleReranker
from agentkit.subagents.builtins import skill_finder
from agentkit.subagents.builtins.knowledge_researcher import (
    KNOWLEDGE_RESEARCHER_SUBAGENT,
)
from agentkit.subagents.builtins.memory_researcher import MEMORY_RESEARCHER_SUBAGENT
from agentkit.subagents.registry import (
    KNOWLEDGE_SUBAGENT_NAME,
    MEMORY_SUBAGENT_NAME,
    SKILL_SUBAGENT_NAME,
    list_subagent_names,
)
from agentkit.subagents.executor import _subagent_tools


def _app(**overrides) -> AppConfig:
    data = {
        "models": [
            {
                "name": "m",
                "use": "langchain_core.language_models.fake_chat_models.FakeListChatModel",
            }
        ]
    }
    data.update(overrides)
    return AppConfig.model_validate(data)


def _knowledge_config() -> dict:
    return {
        "enabled": True,
        "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
        "embedding": {"base_url": "http://embedding:8000/v1"},
        "vector_store": {"uri": "http://milvus:19530"},
    }


def _memory_config() -> dict:
    return {"enabled": True, "user_id_salt": "s" * 32}


def test_retrieval_subagents_declare_exactly_their_tools() -> None:
    assert KNOWLEDGE_RESEARCHER_SUBAGENT.name == KNOWLEDGE_SUBAGENT_NAME
    assert KNOWLEDGE_RESEARCHER_SUBAGENT.tools == ["knowledge_search"]
    assert MEMORY_RESEARCHER_SUBAGENT.name == MEMORY_SUBAGENT_NAME
    assert MEMORY_RESEARCHER_SUBAGENT.tools == [
        "memory_search",
        "memory_read",
        "memory_overview",
    ]
    assert skill_finder.SKILL_FINDER_SUBAGENT.name == SKILL_SUBAGENT_NAME
    assert skill_finder.SKILL_FINDER_SUBAGENT.tools == ["search_and_load_skills"]
    for config in (
        KNOWLEDGE_RESEARCHER_SUBAGENT,
        MEMORY_RESEARCHER_SUBAGENT,
        skill_finder.SKILL_FINDER_SUBAGENT,
    ):
        assert "task" in (config.disallowed_tools or [])
        assert "ask_clarification" in (config.disallowed_tools or [])
        assert config.skills == []


def test_retrieval_subagents_spell_out_when_to_use_and_when_not() -> None:
    for config in (
        KNOWLEDGE_RESEARCHER_SUBAGENT,
        MEMORY_RESEARCHER_SUBAGENT,
        skill_finder.SKILL_FINDER_SUBAGENT,
    ):
        text = config.system_prompt or ""
        for section in (
            "角色与边界",
            "最佳实践",
            "结果怎么写",
            "失败与停止",
        ):
            assert section in text, (config.name, section)
        assert "何时该用本子代理" not in text
        assert "lead 的判断依据" not in text
        assert "不能继续委派" in text or "不能委派" in text

    for config in (
        KNOWLEDGE_RESEARCHER_SUBAGENT,
        MEMORY_RESEARCHER_SUBAGENT,
        skill_finder.SKILL_FINDER_SUBAGENT,
    ):
        assert "不要委派" in config.description
        assert "时委派" in config.description

    memory_prompt = MEMORY_RESEARCHER_SUBAGENT.system_prompt or ""
    assert "experiences" in memory_prompt
    assert "cases" in memory_prompt

    skill_description = skill_finder.DESCRIPTION
    assert "read_file" in skill_description
    assert "工具权限" in skill_description
    assert "看技能索引" not in skill_finder.SYSTEM_PROMPT


def test_registry_hides_subagents_when_their_feature_is_off() -> None:
    disabled = list_subagent_names(app_config=_app(), allowed=None)
    assert KNOWLEDGE_SUBAGENT_NAME not in disabled
    assert MEMORY_SUBAGENT_NAME not in disabled
    assert SKILL_SUBAGENT_NAME in disabled  # skills.search.enabled defaults to true

    enabled = list_subagent_names(
        app_config=_app(
            knowledge_base=_knowledge_config(),
            memory={"openviking": _memory_config()},
            skills={"search": {"enabled": False}},
        ),
        allowed=None,
    )
    assert KNOWLEDGE_SUBAGENT_NAME in enabled
    assert MEMORY_SUBAGENT_NAME in enabled
    assert SKILL_SUBAGENT_NAME not in enabled


def _dispatch(*, tools, delegated_tools, declared):
    from agentkit.subagents.config import INHERIT_MODEL, SubagentConfig

    config = SubagentConfig(
        name="x",
        description="x",
        system_prompt="x",
        tools=declared,
        disallowed_tools=["task"],
        skills=[],
        model=INHERIT_MODEL,
        max_turns=5,
        timeout_seconds=60,
    )
    return SimpleNamespace(
        config=config, tools=list(tools), delegated_tools=list(delegated_tools)
    )


def test_delegated_tools_reach_only_types_that_declare_them() -> None:
    lead_tool = SimpleNamespace(name="read_file")
    knowledge = SimpleNamespace(name="knowledge_search")
    memory = SimpleNamespace(name="memory_search")

    inherited = _subagent_tools(
        _dispatch(tools=[lead_tool], delegated_tools=[knowledge], declared=None)
    )
    assert [tool.name for tool in inherited] == ["read_file"]

    declared = _subagent_tools(
        _dispatch(
            tools=[lead_tool],
            delegated_tools=[knowledge, memory],
            declared=["knowledge_search"],
        )
    )
    assert [tool.name for tool in declared] == ["knowledge_search"]

    both = _subagent_tools(
        _dispatch(
            tools=[lead_tool],
            delegated_tools=[knowledge, memory],
            declared=["read_file", "memory_search"],
        )
    )
    assert [tool.name for tool in both] == ["read_file", "memory_search"]


def _assemble(monkeypatch, *, subagent_enabled: bool, allowed: list[str] | None = None) -> dict:
    captured: dict = {}
    delegated: dict = {}
    memory_tool = SimpleNamespace(name="memory_search")
    skills_tool = SimpleNamespace(name="search_and_load_skills")

    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.get_available_tools",
        lambda *args, **kwargs: [skills_tool],
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_agent",
        lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(**kwargs)),
    )
    monkeypatch.setattr(
        "agentkit.agents.memory.openviking.runtime.get_openviking_memory_runtime",
        lambda: SimpleNamespace(tools=[memory_tool], facade=object()),
    )

    def fake_task_tool(**kwargs):
        delegated.update(kwargs)
        return SimpleNamespace(name="task")

    monkeypatch.setattr(
        "agentkit.tools.builtin.task_tool.make_task_tool", fake_task_tool
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.make_task_tool",
        fake_task_tool,
        raising=False,
    )
    config = {
        "configurable": {
            "model_name": "m",
            "subagent_enabled": subagent_enabled,
            "allowed_subagents": allowed,
        }
    }
    from agentkit.agents.leader_agent import assemble_agent

    assemble_agent(
        config,
        app_config=_app(
            knowledge_base=_knowledge_config(),
            memory={"openviking": _memory_config()},
            agents={"allowed_subagents": allowed},
        ),
        checkpointer=object(),
        subagent_runtime=SimpleNamespace(config=SimpleNamespace(max_running=2)),
    )
    captured["task_kwargs"] = delegated
    return captured


def test_delegation_moves_retrieval_tools_off_the_lead(monkeypatch) -> None:
    captured = _assemble(monkeypatch, subagent_enabled=True)

    names = [tool.name for tool in captured["tools"]]
    assert "knowledge_search" not in names
    assert "memory_search" not in names
    assert "search_and_load_skills" not in names
    assert "task" in names
    delegated = [tool.name for tool in captured["task_kwargs"]["delegated_tools"]]
    assert delegated == ["memory_search", "knowledge_search", "search_and_load_skills"]


def test_a_whitelist_without_retrieval_subagents_keeps_retrieval_on_the_lead(monkeypatch) -> None:
    """general-purpose 继承不到 delegated 工具：白名单里没有检索子代理时，检索工具必须留给 lead。"""
    captured = _assemble(monkeypatch, subagent_enabled=True, allowed=["general-purpose"])

    names = [tool.name for tool in captured["tools"]]
    assert "task" in names
    assert "knowledge_search" in names
    assert "memory_search" in names
    assert "search_and_load_skills" in names
    assert captured["task_kwargs"]["delegated_tools"] == []


def test_only_claimed_retrieval_tools_leave_the_lead(monkeypatch) -> None:
    captured = _assemble(monkeypatch, subagent_enabled=True, allowed=["knowledge-researcher"])

    names = [tool.name for tool in captured["tools"]]
    assert "knowledge_search" not in names
    assert "memory_search" in names
    assert "search_and_load_skills" in names
    delegated = [tool.name for tool in captured["task_kwargs"]["delegated_tools"]]
    assert delegated == ["knowledge_search"]


def test_no_available_subagent_means_no_task_tool(monkeypatch) -> None:
    """白名单一个都对不上：不注册一个指向空菜单的 task，检索工具留给 lead。"""
    captured = _assemble(monkeypatch, subagent_enabled=True, allowed=["nope"])

    names = [tool.name for tool in captured["tools"]]
    assert "task" not in names
    assert "knowledge_search" in names
    assert "memory_search" in names
    assert "search_and_load_skills" in names


def test_delegation_hard_reject_falls_back_to_direct_tools(monkeypatch) -> None:
    captured = _assemble(monkeypatch, subagent_enabled=True, allowed=[])

    names = [tool.name for tool in captured["tools"]]
    assert "task" not in names
    assert "knowledge_search" in names
    assert "memory_search" in names
    assert "search_and_load_skills" in names


def test_delegation_disabled_keeps_direct_tools(monkeypatch) -> None:
    captured = _assemble(monkeypatch, subagent_enabled=False)

    names = [tool.name for tool in captured["tools"]]
    assert "task" not in names
    assert "knowledge_search" in names


def test_prompt_lists_subagents_only_when_delegation_is_active() -> None:
    config = _app(
        knowledge_base=_knowledge_config(),
        memory={"openviking": _memory_config()},
    )

    delegating = prompt_module.apply_prompt_template(
        app_config=config, subagent_enabled=True
    )
    for name in ("knowledge-researcher", "memory-researcher", "skill-finder"):
        assert name in delegating
    assert "不要委派" in delegating
    assert "## 知识库" not in delegating
    assert "## 长期记忆" not in delegating
    assert "knowledge_search" not in delegating
    assert "只能通过对应的检索子代理访问" in delegating

    direct = prompt_module.apply_prompt_template(
        app_config=config, subagent_enabled=False
    )
    assert "knowledge-researcher" not in direct
    assert "knowledge_search" not in direct
    assert "## 知识库" not in direct
    assert "## 长期记忆" not in direct


def _run_in_new_loop(coro_factory):
    result: dict = {}

    def runner() -> None:
        result["value"] = asyncio.run(coro_factory())

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    return result["value"]


@pytest.mark.asyncio
async def test_embedder_creates_a_client_per_event_loop() -> None:
    from agentkit.knowledge.config import EmbeddingConfig

    embedder = OpenAICompatibleEmbedder(
        EmbeddingConfig(base_url="http://embedding:8000/v1")
    )
    first = embedder._http()
    second = await asyncio.to_thread(_run_in_new_loop, lambda: _client_on_other_loop(embedder))
    assert first is not second
    await embedder.aclose()


async def _client_on_other_loop(embedder: OpenAICompatibleEmbedder):
    return embedder._http()


@pytest.mark.asyncio
async def test_reranker_creates_a_client_per_event_loop() -> None:
    from agentkit.knowledge.config import RerankConfig

    reranker = OpenAICompatibleReranker(
        RerankConfig(enabled=True, base_url="http://rerank:8000", model="m")
    )
    first = reranker._http()
    second = await asyncio.to_thread(_run_in_new_loop, lambda: _client_on_other_loop_rerank(reranker))
    assert first is not second
    await reranker.aclose()


async def _client_on_other_loop_rerank(reranker: OpenAICompatibleReranker):
    return reranker._http()


@pytest.mark.asyncio
async def test_openviking_factory_caches_per_loop() -> None:
    from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig

    built: list[tuple[float, str]] = []

    async def builder(config, *, ov_user):
        built.append((asyncio.get_running_loop().time(), ov_user))
        return SimpleNamespace()

    factory = OpenVikingClientFactory(
        OpenVikingMemoryConfig(user_id_salt="s" * 32), builder=builder
    )
    first = await factory.client_for("u1")
    second = await asyncio.to_thread(
        _run_in_new_loop, lambda: factory.client_for("u1")
    )

    assert first is not second
    assert len(built) == 2
    await factory.close()
