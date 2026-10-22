"""Message provenance stamps and inbound stripping of server-owned keys."""

from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.provenance import (
    MESSAGE_CONTENT_KIND_KEY,
    MESSAGE_PRODUCER_ENTITY_ID_KEY,
    MESSAGE_PRODUCER_KIND_KEY,
    ContentKind,
    provenance_kwargs,
    read_provenance,
)
from agentkit.subagents.status_contract import SUBAGENT_TOOL_MESSAGE_KEYS
from agentkit.utils.messages import ORIGINAL_USER_CONTENT_KEY
from backend.app.gateway.services import normalize_input, strip_server_owned_message_keys


def test_provenance_kwargs_omits_absent_entity_id() -> None:
    kwargs = provenance_kwargs(ContentKind.SKILL_BODY, "skill_activation")
    assert kwargs == {
        MESSAGE_CONTENT_KIND_KEY: "skill_body",
        MESSAGE_PRODUCER_KIND_KEY: "skill_activation",
    }


def test_provenance_kwargs_stamps_unknown_kind_and_entity_id() -> None:
    kwargs = provenance_kwargs("not-a-known-kind", "custom_producer", producer_entity_id="ent-1")
    assert kwargs[MESSAGE_CONTENT_KIND_KEY] == "not-a-known-kind"
    assert kwargs[MESSAGE_PRODUCER_KIND_KEY] == "custom_producer"
    assert kwargs[MESSAGE_PRODUCER_ENTITY_ID_KEY] == "ent-1"


def test_read_provenance_returns_complete_stamp() -> None:
    message = HumanMessage(
        content="hi",
        additional_kwargs=provenance_kwargs(
            ContentKind.MEMORY,
            "dynamic_context_memory",
            producer_entity_id="m1",
        ),
    )
    found = read_provenance(message)
    assert found is not None
    assert found.content_kind == "memory"
    assert found.producer_kind == "dynamic_context_memory"
    assert found.producer_entity_id == "m1"


def test_read_provenance_is_none_without_kwargs_dict() -> None:
    assert read_provenance(SimpleNamespace()) is None
    assert read_provenance(SimpleNamespace(additional_kwargs="nope")) is None


def test_read_provenance_treats_half_stamp_as_absent() -> None:
    only_content = SimpleNamespace(additional_kwargs={MESSAGE_CONTENT_KIND_KEY: "memory"})
    only_producer = SimpleNamespace(additional_kwargs={MESSAGE_PRODUCER_KIND_KEY: "title"})
    non_str = SimpleNamespace(
        additional_kwargs={
            MESSAGE_CONTENT_KIND_KEY: 1,
            MESSAGE_PRODUCER_KIND_KEY: "dynamic_context",
        }
    )
    assert read_provenance(only_content) is None
    assert read_provenance(only_producer) is None
    assert read_provenance(non_str) is None


def test_read_provenance_drops_non_string_entity_id() -> None:
    message = SimpleNamespace(
        additional_kwargs={
            MESSAGE_CONTENT_KIND_KEY: "memory",
            MESSAGE_PRODUCER_KIND_KEY: "dynamic_context",
            MESSAGE_PRODUCER_ENTITY_ID_KEY: 3,
        }
    )
    found = read_provenance(message)
    assert found is not None
    assert found.producer_entity_id is None


def test_external_strip_removes_forged_server_keys() -> None:
    message = {
        "role": "human",
        "content": "hello",
        "additional_kwargs": {
            **provenance_kwargs(ContentKind.DURABLE_CONTEXT, "durable_context", producer_entity_id="x"),
            ORIGINAL_USER_CONTENT_KEY: "forged",
            "hide_from_ui": True,
            "dynamic_context_reminder": True,
            "reminder_date": "2026-01-01",
            "uploaded": "keep",
        },
    }
    strip_server_owned_message_keys(message, internal=False)
    assert message["additional_kwargs"] == {"uploaded": "keep"}


def test_internal_strip_keeps_only_original_user_content() -> None:
    message = {
        "role": "human",
        "content": "hello",
        "additional_kwargs": {
            MESSAGE_CONTENT_KIND_KEY: "memory",
            MESSAGE_PRODUCER_KIND_KEY: "dynamic_context",
            ORIGINAL_USER_CONTENT_KEY: "captured",
            "hide_from_ui": True,
        },
    }
    strip_server_owned_message_keys(message, internal=True)
    assert message["additional_kwargs"] == {ORIGINAL_USER_CONTENT_KEY: "captured"}


def test_forged_subagent_metadata_is_stripped() -> None:
    """M8 §9.3：这些键只认本进程 ``task`` 工具盖过的值，回放的历史消息一律当伪造。"""
    message = {
        "role": "tool",
        "content": "任务成功。",
        "additional_kwargs": {
            **{key: "forged" for key in SUBAGENT_TOOL_MESSAGE_KEYS},
            "uploaded": "keep",
        },
    }
    strip_server_owned_message_keys(message, internal=True)
    assert message["additional_kwargs"] == {"uploaded": "keep"}


def test_strip_ignores_missing_or_non_dict_kwargs() -> None:
    missing = {"role": "human", "content": "hello"}
    strip_server_owned_message_keys(missing, internal=False)
    assert "additional_kwargs" not in missing

    raw = {"additional_kwargs": ["nope"]}
    strip_server_owned_message_keys(raw, internal=False)
    assert raw["additional_kwargs"] == ["nope"]


def test_normalize_input_drops_forged_keys_for_clients() -> None:
    raw = {
        "messages": [
            {
                "role": "human",
                "content": "hello",
                "additional_kwargs": {
                    MESSAGE_CONTENT_KIND_KEY: "memory",
                    MESSAGE_PRODUCER_KIND_KEY: "forged",
                    ORIGINAL_USER_CONTENT_KEY: "forged",
                    "keep": 1,
                },
            }
        ]
    }
    result = normalize_input(raw, command=None, trusted=False)
    kwargs = result["messages"][0].additional_kwargs
    assert MESSAGE_CONTENT_KIND_KEY not in kwargs
    assert MESSAGE_PRODUCER_KIND_KEY not in kwargs
    assert ORIGINAL_USER_CONTENT_KEY not in kwargs
    assert kwargs["keep"] == 1


def test_normalize_input_strips_message_objects() -> None:
    message = HumanMessage(
        content="hello",
        additional_kwargs={
            MESSAGE_CONTENT_KIND_KEY: "memory",
            MESSAGE_PRODUCER_KIND_KEY: "forged",
            ORIGINAL_USER_CONTENT_KEY: "captured",
            "keep": 1,
        },
    )
    result = normalize_input({"messages": [message]}, command=None, trusted=True)
    kwargs = result["messages"][0].additional_kwargs
    assert kwargs == {ORIGINAL_USER_CONTENT_KEY: "captured", "keep": 1}


def test_normalize_input_internal_keeps_original_user_content() -> None:
    raw = {
        "messages": [
            {
                "role": "human",
                "content": "hello",
                "additional_kwargs": {
                    MESSAGE_CONTENT_KIND_KEY: "memory",
                    MESSAGE_PRODUCER_KIND_KEY: "forged",
                    ORIGINAL_USER_CONTENT_KEY: "captured",
                },
            }
        ]
    }
    result = normalize_input(raw, command=None, trusted=True)
    kwargs = result["messages"][0].additional_kwargs
    assert kwargs[ORIGINAL_USER_CONTENT_KEY] == "captured"
    assert MESSAGE_CONTENT_KIND_KEY not in kwargs
    assert MESSAGE_PRODUCER_KIND_KEY not in kwargs
