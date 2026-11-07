"""Fixed GATEWAY_WORKERS=1: a ContextForge runtime is process-level by design.

One /admin/events subscriber, one midnight task and one sync lock per process is
the whole concurrency story (rev.9 6.4).  Running that runtime in two gateway
workers would give every worker its own subscriber and its own midnight timer
against the same catalog, so a multi-worker deployment with the integration
enabled must refuse to start instead of quietly double-syncing.
"""

from __future__ import annotations

import pytest

from agentkit.config import AppConfig
from agentkit.config.contextforge_config import ContextForgeConfig
from backend.app.gateway.app import preflight_contextforge


def _enabled() -> ContextForgeConfig:
    return ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="adm",
        search={"enabled": False},
    )


def test_enabled_contextforge_with_multiple_workers_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")

    with pytest.raises(RuntimeError, match="GATEWAY_WORKERS"):
        preflight_contextforge(AppConfig(contextforge=_enabled()))


def test_enabled_contextforge_with_one_worker_passes(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "1")

    preflight_contextforge(AppConfig(contextforge=_enabled()))


def test_disabled_contextforge_does_not_constrain_the_worker_count(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")

    preflight_contextforge(AppConfig())


def test_the_guard_reads_the_environment_not_a_cached_worker_count(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "3")

    with pytest.raises(RuntimeError, match="GATEWAY_WORKERS"):
        preflight_contextforge(AppConfig(contextforge=_enabled()))

    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    preflight_contextforge(AppConfig(contextforge=_enabled()))
