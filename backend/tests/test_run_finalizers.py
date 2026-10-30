"""Run finalizers run for normal completion and cancellation."""

from __future__ import annotations

import pytest

from agentkit.runtime.run_finalizers import (
    clear_run_finalizers,
    finish_run_finalizers,
    pending_finalizers,
    register_run_finalizer,
    unregister_run_finalizer,
)


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_run_finalizers("run-1")
    yield
    clear_run_finalizers("run-1")


@pytest.mark.asyncio
async def test_finalizer_receives_terminal_status_and_is_consumed() -> None:
    seen: list[str] = []

    async def finalize(status: str) -> None:
        seen.append(status)

    register_run_finalizer("run-1", finalize)
    assert pending_finalizers("run-1") == 1

    await finish_run_finalizers("run-1", status="interrupted")

    assert seen == ["interrupted"]
    assert pending_finalizers("run-1") == 0


@pytest.mark.asyncio
async def test_finalizer_failure_does_not_stop_other_finalizers() -> None:
    seen: list[str] = []

    async def broken(_status: str) -> None:
        raise RuntimeError("boom")

    async def finalize(status: str) -> None:
        seen.append(status)

    register_run_finalizer("run-1", broken)
    register_run_finalizer("run-1", finalize)

    await finish_run_finalizers("run-1", status="error")

    assert seen == ["error"]


@pytest.mark.asyncio
async def test_unregister_removes_a_completed_turn() -> None:
    async def finalize(_status: str) -> None:
        return None

    register_run_finalizer("run-1", finalize)
    unregister_run_finalizer("run-1", finalize)

    assert pending_finalizers("run-1") == 0
