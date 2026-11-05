"""Deterministic commit-signal detection for a captured turn."""

from __future__ import annotations

import pytest

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
    detector = CommitSignalDetector()

    signals = await detector.detect(SignalContext(user_text="请记住我偏好简短回答"))

    assert signals == ("EXPLICIT_REMEMBER",)


@pytest.mark.asyncio
async def test_a_question_about_remembering_is_not_explicit_remember() -> None:
    detector = CommitSignalDetector()

    signals = await detector.detect(SignalContext(user_text="记住这个吗？"))

    assert signals == ()


@pytest.mark.asyncio
async def test_assistant_acknowledgement_is_not_explicit_remember() -> None:
    detector = CommitSignalDetector()

    signals = await detector.detect(
        SignalContext(
            user_text="我喜欢简短回答",
            assistant_text="好的，我会记住。",
        )
    )

    assert signals == ()


@pytest.mark.asyncio
async def test_user_correction_is_detected() -> None:
    detector = CommitSignalDetector()

    signals = await detector.detect(SignalContext(user_text="不对，应该是周三"))

    assert signals == ("USER_CORRECTION",)


@pytest.mark.asyncio
async def test_task_completion_is_detected_without_treating_acknowledgements_as_completion() -> None:
    detector = CommitSignalDetector()

    complete = await detector.detect(SignalContext(user_text="好了，完成了"))
    acknowledgement = await detector.detect(SignalContext(user_text="好的，继续"))

    assert complete == ("TASK_COMPLETE",)
    assert acknowledgement == ()


@pytest.mark.asyncio
async def test_failure_recovery_uses_run_evidence() -> None:
    detector = CommitSignalDetector()

    signals = await detector.detect(
        SignalContext(has_tool_error=True, has_recovery=True)
    )

    assert signals == ("FAILURE_RECOVERY",)


@pytest.mark.asyncio
async def test_signals_are_returned_in_priority_order() -> None:
    detector = CommitSignalDetector()

    signals = await detector.detect(
        SignalContext(user_text="不对，请记住我喜欢吃苹果")
    )

    assert signals == ("EXPLICIT_REMEMBER", "USER_CORRECTION")
