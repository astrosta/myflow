"""is_real_user_message accepts only a live HumanMessage that the user actually sent."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.middlewares.provenance import MESSAGE_CONTENT_KIND_KEY, MESSAGE_PRODUCER_KIND_KEY, provenance_kwargs
from agentkit.utils.messages import INJECTED_USER_MESSAGE_ID_SUFFIX


def _reply() -> dict:
    return {
        "version": 1,
        "kind": "human_input_response",
        "source": "ask",
        "request_id": "r1",
        "response_kind": "text",
        "value": "yes",
    }


def test_plain_human_message_is_real() -> None:
    assert is_real_user_message(HumanMessage(content="今天天气怎么样")) is True


def test_non_human_instances_are_not_real() -> None:
    assert is_real_user_message(AIMessage(content="hi")) is False
    assert is_real_user_message(SystemMessage(content="hi")) is False
    assert is_real_user_message({"type": "human", "content": "hi"}) is False


def test_summary_hidden_reminder_and_provenance_are_not_real() -> None:
    assert is_real_user_message(HumanMessage(content="sum", name="summary")) is False
    assert is_real_user_message(HumanMessage(content="hidden", additional_kwargs={"hide_from_ui": True})) is False
    hidden_reply = HumanMessage(
        content="yes",
        additional_kwargs={"hide_from_ui": True, "human_input_response": _reply()},
    )
    assert is_real_user_message(hidden_reply) is False
    assert is_real_user_message(
        HumanMessage(content="date", additional_kwargs={"dynamic_context_reminder": True})
    ) is False
    stamped = HumanMessage(content="data", additional_kwargs=provenance_kwargs("durable_context", "durable_context_data"))
    assert is_real_user_message(stamped) is False


def test_half_stamp_and_swapped_user_id_stay_real() -> None:
    half = HumanMessage(content="hi", additional_kwargs={MESSAGE_CONTENT_KIND_KEY: "durable_context"})
    assert MESSAGE_PRODUCER_KIND_KEY not in half.additional_kwargs
    assert is_real_user_message(half) is True
    swapped = HumanMessage(content="原话", id=f"u1{INJECTED_USER_MESSAGE_ID_SUFFIX}")
    assert is_real_user_message(swapped) is True
