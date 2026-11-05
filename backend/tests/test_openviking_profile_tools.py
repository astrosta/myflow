"""The model-facing profile write tool: contract, gating text, and error shapes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime

from agentkit.agents.memory.openviking.profile import ProfileConflict
from agentkit.agents.memory.openviking.profile_tools import (
    TOOL_NAME,
    configure_profile_updater,
    make_profile_tools,
    make_update_user_profile_tool,
    tool_description,
)
from agentkit.agents.memory.profile_schema import PROFILE_FIELD_SPECS


class FakeUpdater:
    def __init__(self, *, error: Exception | None = None, etag: str = "etag-2") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error = error
        self.etag = etag

    async def update_fields(self, user_id: str, fields: dict):
        self.calls.append((user_id, dict(fields)))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(etag=self.etag)


def runtime(context):
    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id=None,
        store=None,
    )


@pytest.mark.asyncio
async def test_tool_writes_validated_fields_for_the_current_user() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {
                "fields": {
                    "identity.preferred_name": " 小林 ",
                    "communication.language": "auto",
                    "communication.format": ["steps", "bullets"],
                },
                "runtime": runtime({"user_id": "u1"}),
                "confirmed": True,
            }
        )
    )

    assert result["ok"] is True
    assert result["updated"] == [
        "identity.preferred_name",
        "communication.language",
        "communication.format",
    ]
    assert result["cleared"] == []
    assert result["profile_etag"] == "etag-2"
    assert updater.calls == [
        (
            "u1",
            {
                "identity.preferred_name": "小林",
                "communication.language": "auto",
                "communication.format": ["steps", "bullets"],
            },
        )
    ]


@pytest.mark.asyncio
async def test_first_call_only_returns_a_confirmation_prompt() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)
    payload = {
        "fields": {"identity.preferred_name": "小林", "work.role": "后端工程师"},
        "runtime": runtime({"user_id": "u1"}),
    }

    result = json.loads(await tool.ainvoke(payload))

    assert result["ok"] is False
    assert result["status"] == "confirmation_required"
    assert result["interrupt_before"] is True
    assert "小林" in result["message"]
    assert "confirmed=true" in result["message"]
    assert result["updated"] == ["identity.preferred_name", "work.role"]
    assert updater.calls == []

    written = json.loads(await tool.ainvoke({**payload, "confirmed": True}))
    assert written["ok"] is True
    assert updater.calls == [
        ("u1", {"identity.preferred_name": "小林", "work.role": "后端工程师"})
    ]


@pytest.mark.asyncio
async def test_none_clears_a_field() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {
                "fields": {"identity.preferred_name": None},
                "runtime": runtime({"user_id": "u1"}),
                "confirmed": True,
            }
        )
    )

    assert result["updated"] == []
    assert result["cleared"] == ["identity.preferred_name"]
    assert updater.calls[0][1] == {"identity.preferred_name": None}


@pytest.mark.asyncio
async def test_unknown_field_returns_the_allowed_list_and_writes_nothing() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {
                "fields": {"identity.favourite_colour": "blue"},
                "runtime": runtime({"user_id": "u1"}),
            }
        )
    )

    assert result["error"] == "invalid_field"
    assert "favourite_colour" in result["message"]
    assert set(result["allowed_fields"]) == {spec.path for spec in PROFILE_FIELD_SPECS}
    assert updater.calls == []


@pytest.mark.asyncio
async def test_invalid_value_lists_the_allowed_options_and_writes_nothing() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {
                "fields": {"communication.tone": "sarcastic"},
                "runtime": runtime({"user_id": "u1"}),
            }
        )
    )

    assert result["error"] == "invalid_value"
    assert "neutral" in result["message"]
    assert updater.calls == []


@pytest.mark.asyncio
async def test_a_secret_shaped_value_is_rejected_before_storage() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {
                "fields": {"boundaries": ["sk-" + "a" * 24]},
                "runtime": runtime({"user_id": "u1"}),
            }
        )
    )

    assert result["error"] == "invalid_value"
    assert "credential" in result["message"]
    assert updater.calls == []


@pytest.mark.asyncio
async def test_missing_user_is_reported_and_not_written() -> None:
    updater = FakeUpdater()
    tool = make_update_user_profile_tool(updater)

    result = json.loads(
        await tool.ainvoke(
            {"fields": {"work.role": "后端工程师"}, "runtime": runtime({})}
        )
    )

    assert result["error"] == "missing_user"
    assert updater.calls == []


@pytest.mark.asyncio
async def test_etag_conflict_and_storage_failure_become_tool_errors() -> None:
    conflict = make_update_user_profile_tool(FakeUpdater(error=ProfileConflict("stale")))
    crashed = make_update_user_profile_tool(FakeUpdater(error=RuntimeError("db down")))
    payload = {
        "fields": {"work.role": "后端工程师"},
        "runtime": runtime({"user_id": "u1"}),
        "confirmed": True,
    }

    assert json.loads(await conflict.ainvoke(payload))["error"] == "profile_update_failed"
    assert json.loads(await crashed.ainvoke(payload))["error"] == "profile_update_failed"


def test_description_names_every_field_and_the_call_gates() -> None:
    description = tool_description()

    for spec in PROFILE_FIELD_SPECS:
        assert spec.path in description
    for phrase in (
        "以后叫我",
        "以后都用英文",
        "这次用英文",
        "一次性的任务指令",
        "推测",
        "密钥",
        "null",
        "interrupt_before",
        "confirmed=true",
    ):
        assert phrase in description
    # 工具名与注册名一致，避免 description 与实际入口漂移，
    # 并声明这是需要先确认再写入的工具。
    tool = make_update_user_profile_tool(FakeUpdater())
    assert tool.name == TOOL_NAME
    assert tool.extras == {"interrupt_before": True}


def test_tools_are_registered_only_after_a_writer_is_wired() -> None:
    configure_profile_updater(None)
    assert make_profile_tools() == []

    updater = FakeUpdater()
    configure_profile_updater(updater)
    try:
        tools = make_profile_tools()
        assert [tool.name for tool in tools] == [TOOL_NAME]
    finally:
        configure_profile_updater(None)
