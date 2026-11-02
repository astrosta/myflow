"""Agent assembly registers the profile write tool from the profile service."""

from __future__ import annotations

from types import SimpleNamespace

from agentkit.agents.leader_agent import assemble_agent
from agentkit.agents.memory.openviking.profile_tools import configure_profile_updater
from agentkit.config import AppConfig


class FakeProfileService:
    async def update_fields(self, user_id, fields):
        return SimpleNamespace(etag="etag")


def _app(*, memory_enabled: bool) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "m",
                    "use": "langchain_core.language_models.fake_chat_models.FakeListChatModel",
                }
            ],
            "memory": {
                "openviking": {
                    "enabled": memory_enabled,
                    "user_id_salt": "s" * 32,
                    "capture_stream": {"redis_url": "redis://localhost:6379/0"},
                }
            },
        }
    )


def _assemble(monkeypatch, *, memory_enabled: bool) -> dict:
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
        {"configurable": {"model_name": "m", "subagent_enabled": False}},
        app_config=_app(memory_enabled=memory_enabled),
        checkpointer=object(),
    )
    return captured


def test_profile_tool_is_attached_even_when_memory_is_off(monkeypatch) -> None:
    configure_profile_updater(FakeProfileService())
    try:
        captured = _assemble(monkeypatch, memory_enabled=False)
    finally:
        configure_profile_updater(None)

    assert [tool.name for tool in captured["tools"]] == ["update_user_profile"]


def test_no_profile_service_means_no_profile_tool(monkeypatch) -> None:
    configure_profile_updater(None)

    captured = _assemble(monkeypatch, memory_enabled=False)

    assert captured["tools"] == []


def test_memory_switch_still_adds_memory_tools_alongside_the_profile_tool(monkeypatch) -> None:
    memory_tool = SimpleNamespace(name="memory_search")
    configure_profile_updater(FakeProfileService())
    try:
        captured = _assemble_with_memory(monkeypatch, memory_tool)
    finally:
        configure_profile_updater(None)

    names = [tool.name for tool in captured["tools"]]
    assert names == ["memory_search", "update_user_profile"]


def _assemble_with_memory(monkeypatch, memory_tool) -> dict:
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
    monkeypatch.setattr(
        "agentkit.agents.memory.openviking.runtime.get_openviking_memory_runtime",
        lambda: SimpleNamespace(tools=[memory_tool], facade=object()),
    )
    assemble_agent(
        {"configurable": {"model_name": "m", "subagent_enabled": False}},
        app_config=_app(memory_enabled=True),
        checkpointer=object(),
    )
    return captured
