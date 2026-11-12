"""Strict chat templates accept exactly one leading system message."""

import asyncio

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agentkit.agents.middlewares.assembly import (
    build_middlewares,
    build_subagent_runtime_middlewares,
)
from agentkit.agents.middlewares.dynamic_context_middleware import (
    DynamicContextMiddleware,
)
from agentkit.agents.middlewares.system_message_coalescing_middleware import (
    SystemMessageCoalescingMiddleware,
)
from agentkit.agents.thread_state import ThreadState
from agentkit.config import AppConfig
from fakes.chat_model import FakeChatModel


def _reminder(content: str, date: str) -> SystemMessage:
    return SystemMessage(
        content=content,
        additional_kwargs={
            "dynamic_context_reminder": True,
            "reminder_date": date,
        },
    )


def _request(
    messages: list,
    *,
    system_message: SystemMessage | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=list(messages),
        system_message=system_message,
        state={"messages": list(messages)},
    )


def test_prompt_and_injected_system_messages_coalesce_into_one_leading_message() -> None:
    prompt = SystemMessage(content="STATIC PROMPT")
    old_reminder = _reminder("<current_date>2026-01-01</current_date>", "2026-01-01")
    latest_reminder = _reminder("<current_date>2026-01-02</current_date>", "2026-01-02")
    contract = SystemMessage(content="DURABLE CONTRACT")
    first_user = HumanMessage(content="q1", id="u1")
    answer = AIMessage(content="a1", id="a1")
    second_user = HumanMessage(content="q2", id="u2")
    request = _request(
        [old_reminder, first_user, answer, latest_reminder, second_user, contract],
        system_message=prompt,
    )
    seen: dict = {}

    def handler(seen_request: ModelRequest) -> str:
        seen["request"] = seen_request
        return "ok"

    result = SystemMessageCoalescingMiddleware().wrap_model_call(request, handler)

    assert result == "ok"
    merged = seen["request"]
    assert isinstance(merged.system_message, SystemMessage)
    assert merged.system_message.content == (
        "STATIC PROMPT\n\n"
        "<current_date>2026-01-02</current_date>\n\n"
        "DURABLE CONTRACT"
    )
    assert not any(isinstance(message, SystemMessage) for message in merged.messages)
    assert merged.messages == [first_user, answer, second_user]
    # Only the request is changed: the graph state still owns every original row.
    assert request.messages[0] is old_reminder
    assert request.messages[3] is latest_reminder


def test_a_system_message_in_the_initial_state_becomes_the_request_prompt() -> None:
    role = SystemMessage(content="SUBAGENT ROLE")
    task = HumanMessage(content="do the task", id="u1")
    request = _request([role, task])
    seen: dict = {}

    def handler(seen_request: ModelRequest) -> str:
        seen["request"] = seen_request
        return "ok"

    SystemMessageCoalescingMiddleware().wrap_model_call(request, handler)

    assert seen["request"].system_message is not None
    assert seen["request"].system_message.content == "SUBAGENT ROLE"
    assert seen["request"].messages == [task]


def test_async_model_calls_are_normalized_too() -> None:
    prompt = SystemMessage(content="PROMPT")
    reminder = _reminder("<current_date>2026-01-02</current_date>", "2026-01-02")
    user = HumanMessage(content="hi", id="u1")
    request = _request([reminder, user], system_message=prompt)
    seen: dict = {}

    async def handler(seen_request: ModelRequest) -> str:
        seen["request"] = seen_request
        return "ok"

    result = asyncio.run(
        SystemMessageCoalescingMiddleware().awrap_model_call(request, handler)
    )

    assert result == "ok"
    assert seen["request"].system_message.content == (
        "PROMPT\n\n<current_date>2026-01-02</current_date>"
    )
    assert seen["request"].messages == [user]


class _InjectsSystemMessage(AgentMiddleware):
    def wrap_model_call(self, request: ModelRequest, handler) -> str:
        injected = SystemMessage(content="INJECTED")
        return handler(request.override(messages=[injected, *request.messages]))


class _RecordingChatModel(FakeChatModel):
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen = [*self.seen, list(messages)]
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_both_agent_chains_put_coalescing_last() -> None:
    app_config = AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]
    )

    lead = build_middlewares({}, "m", app_config=app_config)
    subagent = build_subagent_runtime_middlewares(app_config)

    assert isinstance(lead[-1], SystemMessageCoalescingMiddleware)
    assert isinstance(subagent[-1], SystemMessageCoalescingMiddleware)


def test_the_dynamic_date_reminder_is_part_of_the_single_system_message() -> None:
    model = _RecordingChatModel(reply="ok")
    graph = create_agent(
        model=model,
        tools=[],
        middleware=[DynamicContextMiddleware(), SystemMessageCoalescingMiddleware()],
        system_prompt="BASE PROMPT",
        state_schema=ThreadState,
    )

    graph.invoke({"messages": [HumanMessage(content="hi", id="u1")]})

    provider_messages = model.seen[0]
    assert [message.type for message in provider_messages] == ["system", "human"]
    assert provider_messages[0].content.startswith(
        "BASE PROMPT\n\n<system-reminder>\n<current_date>"
    )


def test_a_system_message_in_initial_state_is_promoted_for_the_provider() -> None:
    """Subagents keep their role prompt in state instead of create_agent's prompt."""
    model = _RecordingChatModel(reply="ok")
    graph = create_agent(
        model=model,
        tools=[],
        middleware=[SystemMessageCoalescingMiddleware()],
        system_prompt=None,
        state_schema=ThreadState,
    )

    graph.invoke(
        {
            "messages": [
                SystemMessage(content="SUBAGENT ROLE"),
                HumanMessage(content="task", id="u1"),
            ]
        }
    )

    provider_messages = model.seen[0]
    assert [message.type for message in provider_messages] == ["system", "human"]
    assert provider_messages[0].content == "SUBAGENT ROLE"


def test_the_provider_receives_one_leading_system_message() -> None:
    """The graph's system prompt and a middleware injection arrive as one message."""
    model = _RecordingChatModel(reply="ok")
    graph = create_agent(
        model=model,
        tools=[],
        middleware=[_InjectsSystemMessage(), SystemMessageCoalescingMiddleware()],
        system_prompt="BASE PROMPT",
        state_schema=ThreadState,
    )

    result = graph.invoke({"messages": [HumanMessage(content="hi", id="u1")]})

    provider_messages = model.seen[0]
    assert [message.type for message in provider_messages] == ["system", "human"]
    assert provider_messages[0].content == "BASE PROMPT\n\nINJECTED"
    assert [message.type for message in result["messages"]] == ["human", "ai"]
    assert result["messages"][0].content == "hi"
    assert result["messages"][1].content == "ok"
