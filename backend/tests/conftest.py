"""Shared backend test isolation for process-local provider hooks."""

from __future__ import annotations

import pytest

from agentkit.agents.memory.openviking.profile_tools import configure_profile_updater


@pytest.fixture(autouse=True)
def _reset_profile_updater() -> None:
    """The profile writer is a process singleton; a leaked fake must not follow us.

    Agent assembly reads it on every run, so a test that installs a fake and does
    not clear it would change the tool list of every later test in this worker.
    """

    configure_profile_updater(None)
    yield
    configure_profile_updater(None)
