"""The switch changes the assembled middleware chain, not only the HTTP routes."""

from __future__ import annotations

from types import SimpleNamespace

from agentkit.agents.middlewares.assembly import build_middlewares
from agentkit.agents.middlewares.memory_middleware import MemoryMiddleware
from agentkit.agents.middlewares.openviking_memory_middleware import OpenVikingMemoryMiddleware
from agentkit.agents.middlewares.task_classifier import TaskClassifierMiddleware
from agentkit.agents.memory.openviking.runtime import configure_openviking_memory
from agentkit.config import AppConfig


def _app(*, enabled: bool) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "flash", "use": "langchain_openai:ChatOpenAI"}],
            "memory": {
                "enabled": True,
                "openviking": {
                    "enabled": enabled,
                    "user_id_salt": "s" * 32,
                },
            },
        }
    )


def test_enabled_switch_uses_openviking_and_drops_canonical_memory() -> None:
    configure_openviking_memory(SimpleNamespace(facade=object(), tools=[]))
    try:
        chain = build_middlewares({}, "flash", app_config=_app(enabled=True), user_id="u1")
    finally:
        configure_openviking_memory(None)

    assert any(isinstance(item, OpenVikingMemoryMiddleware) for item in chain)
    assert not any(isinstance(item, MemoryMiddleware) for item in chain)
    assert not any(isinstance(item, TaskClassifierMiddleware) for item in chain)


def test_disabled_switch_does_not_fall_back_to_canonical_memory() -> None:
    chain = build_middlewares({}, "flash", app_config=_app(enabled=False), user_id="u1")

    assert not any(isinstance(item, MemoryMiddleware) for item in chain)
    assert not any(isinstance(item, OpenVikingMemoryMiddleware) for item in chain)
