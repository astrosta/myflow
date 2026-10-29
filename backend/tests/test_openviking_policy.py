"""Deterministic turn policy for capture vs skip."""

from __future__ import annotations

import pytest

from agentkit.agents.memory.openviking.policy import classify_turn


@pytest.mark.parametrize(
    ("text", "disposition"),
    [
        ("你好", "skip"),
        ("您好！", "skip"),
        ("在吗？", "skip"),
        ("最近怎么样", "skip"),
        ("谢谢", "skip"),
        ("辛苦了", "skip"),
        ("👍", "skip"),
        ("好的", "capture"),
        ("收到", "capture"),
        ("可以", "capture"),
        ("继续", "capture"),
        ("完成", "capture"),
        ("好了", "capture"),
        ("我住杭州", "capture"),
        ("你好，帮我改这个函数", "capture"),
        ("谢谢，不过请记住我偏好简短回答", "capture"),
    ],
)
def test_turn_policy(text: str, disposition: str) -> None:
    assert classify_turn(text).disposition == disposition


def test_farewell_emits_session_end() -> None:
    decision = classify_turn("再见")
    assert decision.disposition == "skip"
    assert decision.signals == ("SESSION_END",)


def test_attachment_forces_capture_even_for_social_text() -> None:
    decision = classify_turn("你好", has_attachments=True)
    assert decision.disposition == "capture"


def test_extra_skip_token_is_allowed_only_for_social_tokens() -> None:
    assert classify_turn("嗯", extra_skip_tokens=("嗯",)).disposition == "skip"
    with pytest.raises(ValueError):
        classify_turn("嗯", extra_skip_tokens=("好的",))


def test_switches_can_disable_social_skip() -> None:
    assert classify_turn("你好", skip_greetings=False).disposition == "capture"
    assert classify_turn("👍", skip_emoji_only=False).disposition == "capture"
