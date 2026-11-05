"""Shared backend test isolation for process-local provider hooks."""

from __future__ import annotations

import pytest

from agentkit.agents.memory.openviking.profile_tools import configure_profile_updater
from agentkit.sandbox.lease import reset_sandbox_lease_manager
from agentkit.sandbox.sandbox_provider import reset_sandbox_provider


@pytest.fixture(autouse=True)
def _reset_profile_updater() -> None:
    """The profile writer is a process singleton; a leaked fake must not follow us.

    Agent assembly reads it on every run, so a test that installs a fake and does
    not clear it would change the tool list of every later test in this worker.
    """

    configure_profile_updater(None)
    yield
    configure_profile_updater(None)


@pytest.fixture(autouse=True)
def _reset_sandbox_provider() -> None:
    """Drop the sandbox provider and lease ledger between tests.

    get_sandbox_provider() builds a process singleton from the *ambient* app
    config, so one test that touches a sandbox tool can leave a provider built
    from the developer's live config.yaml behind. Every later test would then
    reuse it - including its remote health checks - and the suite would pass or
    fail depending on what that file happens to point at.
    """

    yield
    reset_sandbox_provider()
    reset_sandbox_lease_manager()
