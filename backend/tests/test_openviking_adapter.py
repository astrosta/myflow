"""LangChain message to OpenViking wire contract."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter


def test_human_payload_has_user_query_kind() -> None:
    payload = MemoryMessageAdapter().to_payloads(HumanMessage(content="你好"))[0]

    assert payload["role"] == "user"
    assert payload["message_kind"] == "user_query"
    assert payload["parts"] == [{"type": "text", "text": "你好"}]


def test_tool_message_is_tool_transport_and_redacted() -> None:
    message = ToolMessage(
        content="token=sk-abcdefghijklmnopqrstuvwxyz",
        tool_call_id="c1",
        name="shell",
    )
    payload = MemoryMessageAdapter().to_payloads(message)[0]

    assert payload["role"] == "assistant"
    assert payload["message_kind"] == "tool_transport"
    tool = payload["parts"][0]
    assert tool["tool_id"] == "c1"
    assert tool["tool_name"] == "shell"
    assert tool["tool_status"] == "completed"
    assert "sk-abcdef" not in tool["tool_output"]
    assert tool["tool_output_sha256"]
    assert tool["tool_output_truncated"] is False


def test_ai_tool_call_keeps_id_and_name() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "shell", "args": {"cmd": "ls"}}],
    )
    parts = MemoryMessageAdapter().to_payloads(message)[0]["parts"]

    assert parts[0]["type"] == "tool"
    assert parts[0]["tool_id"] == "c1"
    assert parts[0]["tool_name"] == "shell"
    assert parts[0]["tool_status"] == "pending"


def test_system_and_empty_are_not_recordable() -> None:
    adapter = MemoryMessageAdapter()

    assert not adapter.is_recordable(SystemMessage(content="system"))
    assert not adapter.is_recordable(HumanMessage(content="  "))
