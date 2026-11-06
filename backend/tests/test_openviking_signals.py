"""Deterministic commit-signal detection for a captured turn."""

from __future__ import annotations

import pytest

import agentkit.agents.memory.openviking.signals as signal_module
from agentkit.agents.memory.openviking.signals import CommitSignalDetector, SignalContext


class SpyModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, prompt: str) -> str:
        self.calls += 1
        return '{"signals": ["TASK_COMPLETE"]}'


@pytest.mark.asyncio
async def test_an_existing_signal_wins_without_calling_the_model() -> None:
    model = SpyModel()
    detector = CommitSignalDetector(model=model)

    signals = await detector.detect(
        SignalContext(
            user_text="不对，请记住我喜欢吃苹果",
            signals=("EXPLICIT_REMEMBER",),
        )
    )

    assert signals == ("EXPLICIT_REMEMBER",)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_explicit_remember_is_detected_from_the_user_message() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(SignalContext(user_text="请记住我偏好简短回答"))

    assert signals == ("EXPLICIT_REMEMBER",)


@pytest.mark.asyncio
async def test_a_question_about_remembering_is_not_explicit_remember() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(SignalContext(user_text="记住这个吗？"))

    assert signals == ()


@pytest.mark.asyncio
async def test_assistant_acknowledgement_is_not_explicit_remember() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(
        SignalContext(
            user_text="我喜欢简短回答",
            assistant_text="好的，我会记住。",
        )
    )

    assert signals == ()


@pytest.mark.asyncio
async def test_user_correction_is_detected() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(SignalContext(user_text="不对，应该是周三"))

    assert signals == ("USER_CORRECTION",)


@pytest.mark.asyncio
async def test_task_completion_is_detected_without_treating_acknowledgements_as_completion() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    complete = await detector.detect(SignalContext(user_text="好了，完成了"))
    acknowledgement = await detector.detect(SignalContext(user_text="好的，继续"))

    assert complete == ("TASK_COMPLETE",)
    assert acknowledgement == ()


@pytest.mark.asyncio
async def test_failure_recovery_uses_run_evidence() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(
        SignalContext(has_tool_error=True, has_recovery=True)
    )

    assert signals == ("FAILURE_RECOVERY",)


@pytest.mark.asyncio
async def test_signals_are_returned_in_priority_order() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(
        SignalContext(user_text="不对，请记住我喜欢吃苹果")
    )

    assert signals == ("EXPLICIT_REMEMBER", "USER_CORRECTION")


def test_create_default_model_uses_the_process_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_chat_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("agentkit.models.factory.create_chat_model", fake_create_chat_model)

    signal_module._create_default_model()

    assert captured == {"thinking_enabled": False, "attach_tracing": False}


@pytest.mark.asyncio
async def test_default_model_factory_is_used_for_ambiguous_turns(monkeypatch) -> None:
    model = SpyModel()
    calls: list[int] = []

    def factory():
        calls.append(1)
        return model

    monkeypatch.setattr(
        "agentkit.agents.memory.openviking.signals._create_default_model", factory
    )
    detector = CommitSignalDetector()

    signals = await detector.detect(SignalContext(user_text="我住在杭州"))

    assert signals == ("TASK_COMPLETE",)
    assert calls == [1]
    assert model.calls == 1


@pytest.mark.asyncio
async def test_default_model_is_built_once_and_only_when_rules_are_silent(monkeypatch) -> None:
    model = SpyModel()
    calls: list[int] = []

    def factory():
        calls.append(1)
        return model

    monkeypatch.setattr(
        "agentkit.agents.memory.openviking.signals._create_default_model", factory
    )
    detector = CommitSignalDetector()

    ruled = await detector.detect(SignalContext(user_text="请记住我偏好简短回答"))
    first = await detector.detect(SignalContext(user_text="我住在杭州"))
    second = await detector.detect(SignalContext(user_text="我平时写 Python"))

    assert ruled == ("EXPLICIT_REMEMBER",)
    assert first == ("TASK_COMPLETE",)
    assert second == ("TASK_COMPLETE",)
    assert calls == [1]
    assert model.calls == 2


@pytest.mark.asyncio
async def test_rule_only_mode_does_not_build_a_model() -> None:
    detector = CommitSignalDetector(use_default_model=False)

    signals = await detector.detect(SignalContext(user_text="我住在杭州"))

    assert signals == ()
