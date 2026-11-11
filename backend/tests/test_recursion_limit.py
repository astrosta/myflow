"""The default recursion budget must count graph super-steps, not model turns."""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agentkit.agents.middlewares.assembly import build_middlewares
from agentkit.agents.thread_state import ThreadState
from agentkit.config import AppConfig
from agentkit.subagents.executor import recursion_limit_for
from backend.app.gateway.services import (
    DEFAULT_RECURSION_LIMIT,
    _clamp_recursion_limit,
    build_run_config,
)


def _lead_graph() -> object:
    return create_agent(
        model=GenericFakeChatModel(messages=iter([AIMessage(content="ok")])),
        tools=[],
        middleware=build_middlewares({}, "model", app_config=AppConfig()),
        state_schema=ThreadState,
        checkpointer=False,
    )


def test_default_recursion_limit_leaves_room_for_fifty_lead_turns() -> None:
    """A 17-node lead graph spends many super-steps per model/tool turn.

    The old default (100) was exhausted after roughly nine turns, even though it
    looked like a 100-turn budget. Tie the default to the real compiled graph so
    a normal task that checks its own output does not die as a "loop".
    """

    graph = _lead_graph()
    assert DEFAULT_RECURSION_LIMIT >= recursion_limit_for(graph, 50)


def test_default_limit_is_clamped_by_an_operators_lower_ceiling() -> None:
    assert _clamp_recursion_limit(None, 50) == 50


def test_run_config_uses_the_same_default_and_ceiling() -> None:
    config = build_run_config(
        "thread-1", None, {}, app_config=AppConfig(max_recursion_limit=50)
    )

    assert config["recursion_limit"] == 50
