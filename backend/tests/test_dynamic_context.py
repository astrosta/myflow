"""Dynamic context keeps the system prompt static and stores the date on a reminder."""

import asyncio
import logging
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from zoneinfo import ZoneInfo

from agentkit.agents.middlewares.dynamic_context_middleware import (
    DynamicContextMiddleware,
    _format_current_date,
)
from agentkit.agents.middlewares.provenance import (
    MESSAGE_CONTENT_KIND_KEY,
    MESSAGE_PRODUCER_KIND_KEY,
)
from agentkit.utils.messages import INJECTED_USER_MESSAGE_ID_SUFFIX

_PROMPT = "LEAD PROMPT BYTES\nDo not rewrite this."


def _prompt() -> SystemMessage:
    return SystemMessage(content=_PROMPT, id="sys")


def test_empty_history_injects_nothing() -> None:
    assert DynamicContextMiddleware().before_agent({"messages": []}, object()) is None


def test_system_prompt_bytes_stay_put_and_the_date_lives_in_the_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    prompt = _prompt()
    human = HumanMessage(content="今天天气怎么样", id="u1", name="user-input", additional_kwargs={"files": []})
    result = DynamicContextMiddleware().before_agent({"messages": [prompt, human]}, object())
    assert prompt.content == _PROMPT
    assert [message.id for message in result["messages"]] == ["u1", f"u1{INJECTED_USER_MESSAGE_ID_SUFFIX}"]
    reminder, copied = result["messages"]
    assert isinstance(reminder, SystemMessage)
    assert reminder.content == "<system-reminder>\n<current_date>2026-05-08 星期五</current_date>\n</system-reminder>"
    assert _PROMPT not in reminder.content
    assert reminder.additional_kwargs["hide_from_ui"] is True
    assert reminder.additional_kwargs["dynamic_context_reminder"] is True
    assert reminder.additional_kwargs["reminder_date"] == "2026-05-08 星期五"
    assert reminder.additional_kwargs[MESSAGE_CONTENT_KIND_KEY] == "middleware_injection"
    assert reminder.additional_kwargs[MESSAGE_PRODUCER_KIND_KEY] == "dynamic_context"
    assert isinstance(copied, HumanMessage)
    assert copied.content == "今天天气怎么样"
    assert copied.name == "user-input"
    assert copied.additional_kwargs == {"files": []}
    assert not any(message.id == "sys" for message in result["messages"])
    assert not any(str(message.id).endswith("__memory") for message in result["messages"])


def test_reminder_attaches_to_the_last_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    summary = HumanMessage(content="摘要", id="s", name="summary")
    earlier = HumanMessage(content="旧问题", id="old")
    swapped = HumanMessage(content="已经换过", id="done__user")
    latest = HumanMessage(content="新问题", id="new")
    result = DynamicContextMiddleware().before_agent(
        {"messages": [_prompt(), summary, earlier, swapped, latest, AIMessage(content="忽略")]},
        object(),
    )
    assert [message.id for message in result["messages"]] == ["new", f"new{INJECTED_USER_MESSAGE_ID_SUFFIX}"]
    assert result["messages"][1].content == "新问题"


def test_same_day_does_not_inject_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    reminder = SystemMessage(
        content="<system-reminder>\n<current_date>2026-05-08 星期五</current_date>\n</system-reminder>",
        id="u1",
        additional_kwargs={"dynamic_context_reminder": True, "reminder_date": "2026-05-08 星期五"},
    )
    user = HumanMessage(content="今天天气怎么样", id=f"u1{INJECTED_USER_MESSAGE_ID_SUFFIX}")
    fresh = HumanMessage(content="再问一句", id="u2")
    assert DynamicContextMiddleware().before_agent({"messages": [reminder, user, fresh]}, object()) is None


def test_midnight_writes_a_new_date_reminder_and_leaves_the_old_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-09 星期六",
    )
    prompt = _prompt()
    old = SystemMessage(
        content="<system-reminder>\n<current_date>2026-05-08 星期五</current_date>\n</system-reminder>",
        id="u1",
        additional_kwargs={"dynamic_context_reminder": True, "reminder_date": "2026-05-08 星期五"},
    )
    previous = HumanMessage(content="昨天的问题", id=f"u1{INJECTED_USER_MESSAGE_ID_SUFFIX}")
    latest = HumanMessage(content="今天的问题", id="u2")
    result = DynamicContextMiddleware().before_agent(
        {"messages": [prompt, old, previous, latest]},
        object(),
    )
    assert prompt.content == _PROMPT
    assert [message.id for message in result["messages"]] == ["u2", f"u2{INJECTED_USER_MESSAGE_ID_SUFFIX}"]
    assert result["messages"][0].content == "<current_date>2026-05-09 星期六</current_date>"
    assert result["messages"][0].additional_kwargs["reminder_date"] == "2026-05-09 星期六"
    assert "<system-reminder>" not in result["messages"][0].content
    assert result["messages"][1].content == "今天的问题"
    assert "u1" not in [message.id for message in result["messages"]]


def test_legacy_system_reminder_date_comes_from_content_not_from_memory_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    legacy = SystemMessage(
        content="<current_date>2026-05-08 星期五</current_date>",
        id="old",
        additional_kwargs={"dynamic_context_reminder": True},
    )
    memory = HumanMessage(
        content="<current_date>1999-01-01 星期五</current_date>",
        id="old__memory",
        additional_kwargs={"dynamic_context_reminder": True},
    )
    assert DynamicContextMiddleware().before_agent(
        {"messages": [legacy, memory, HumanMessage(content="hi", id="u2")]},
        object(),
    ) is None


def test_a_prompt_that_mentions_the_date_tag_is_not_a_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    prompt = SystemMessage(content="Mention <current_date>1999-01-01 星期五</current_date> in docs.", id="sys")
    result = DynamicContextMiddleware().before_agent(
        {"messages": [prompt, HumanMessage(content="hi", id="u1")]},
        object(),
    )
    assert result["messages"][0].additional_kwargs["reminder_date"] == "2026-05-08 星期五"
    assert prompt.content == "Mention <current_date>1999-01-01 星期五</current_date> in docs."


def test_invalid_timezone_falls_back_to_the_server_local_date(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MYFLOW_DATE_TIMEZONE", "Not/AZone")
    with caplog.at_level(logging.WARNING):
        now = datetime.now()
        assert _format_current_date() == now.strftime("%Y-%m-%d") + " 星期" + "一二三四五六日"[now.weekday()]
    assert any("Not/AZone" in record.message for record in caplog.records)


def test_configured_timezone_formats_that_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYFLOW_DATE_TIMEZONE", "UTC")
    now = datetime.now(ZoneInfo("UTC"))
    assert _format_current_date() == now.strftime("%Y-%m-%d") + " 星期" + "一二三四五六日"[now.weekday()]


def test_memory_timeout_still_injects_the_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentkit.agents.middlewares.dynamic_context_middleware._format_current_date",
        lambda: "2026-05-08 星期五",
    )
    monkeypatch.setattr("agentkit.agents.middlewares.dynamic_context_middleware._INJECT_TIMEOUT_SECONDS", 0.05)

    def slow(self, state, runtime) -> str:
        import time

        time.sleep(1)
        return "<memory>should not appear</memory>"

    monkeypatch.setattr(DynamicContextMiddleware, "_load_memory", slow)
    middleware = DynamicContextMiddleware()

    async def run() -> dict | None:
        return await middleware.abefore_agent({"messages": [HumanMessage(content="hi", id="u1")]}, object())

    result = asyncio.run(run())
    assert "<current_date>2026-05-08 星期五</current_date>" in result["messages"][0].content
    assert "should not appear" not in result["messages"][0].content
    assert not any(str(message.id).endswith("__memory") for message in result["messages"])
