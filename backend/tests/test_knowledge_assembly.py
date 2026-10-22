"""Agent assembly attaches knowledge_search only when knowledge is enabled."""

from __future__ import annotations

from types import SimpleNamespace

from agentkit.agents.leader_agent import assemble_agent
from agentkit.config import AppConfig


def _app(enabled: bool) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "m",
                    "use": "langchain_core.language_models.fake_chat_models.FakeListChatModel",
                }
            ],
            "knowledge_base": {
                "enabled": enabled,
                "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
                "embedding": {"base_url": "http://embedding:8000/v1"},
                "vector_store": {"uri": "http://milvus:19530"},
            },
        }
    )


def _assemble(monkeypatch, enabled: bool) -> dict:
    captured: dict = {}
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
        lambda **kwargs: (captured.update(kwargs) or SimpleNamespace(**kwargs)),
    )
    assemble_agent(
        {"configurable": {"model_name": "m"}},
        app_config=_app(enabled),
        checkpointer=object(),
    )
    return captured


def test_enabled_knowledge_attaches_tool_and_prompt(monkeypatch) -> None:
    captured = _assemble(monkeypatch, True)
    names = [item.name for item in captured["tools"]]
    assert "knowledge_search" in names
    assert "## 知识库" in captured["system_prompt"]


def test_disabled_knowledge_adds_nothing(monkeypatch) -> None:
    captured = _assemble(monkeypatch, False)
    assert [item.name for item in captured["tools"]] == []
    assert "## 知识库" not in captured["system_prompt"]
