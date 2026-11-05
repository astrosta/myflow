from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS


def _models() -> list[dict]:
    return [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]


def _e2b(**overrides) -> dict:
    data = {
        "api_url": "http://127.0.0.1:3000",
        "template_id": "tpl-1",
        "lease": {"redis_url": "redis://127.0.0.1:6379/0", "ttl_seconds": 30},
    }
    data.update(overrides)
    return data


def test_local_config_does_not_require_e2b_section() -> None:
    cfg = AppConfig.model_validate({"models": _models()})
    assert cfg.sandbox.e2b is None


def test_e2b_config_requires_template_and_lease() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"e2b": {"api_url": "http://x"}}}
        )


def test_e2b_config_rejects_host_mount() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "sandbox": {
                    "e2b": _e2b(
                        host_mount={"local_root": "/opt/myflow"}
                    )
                },
            }
        )


def test_e2b_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"e2b": _e2b(unknown="x")}}
        )


def test_e2b_section_is_startup_only() -> None:
    assert "sandbox.e2b" in STARTUP_ONLY_FIELDS


def test_e2b_config_models_are_exported() -> None:
    from agentkit.config import (
        E2BSandboxConfig,
        E2BSandboxNetworkConfig,
        E2BSandboxSyncConfig,
        SandboxLeaseConfig,
    )

    assert E2BSandboxConfig is not None
    assert E2BSandboxNetworkConfig is not None
    assert E2BSandboxSyncConfig is not None
    assert SandboxLeaseConfig is not None


def test_e2b_config_documented_defaults() -> None:
    cfg = AppConfig.model_validate(
        {"models": _models(), "sandbox": {"e2b": _e2b()}}
    )

    parsed = cfg.sandbox.e2b
    assert parsed is not None
    assert parsed.api_url == "http://127.0.0.1:3000"
    assert parsed.sandbox_domain == "cube.app"
    assert parsed.timeout_seconds == 1800
    assert parsed.on_timeout == "pause"
    assert parsed.auto_resume is True
    assert parsed.release_action == "pause"
    assert parsed.acquire_timeout_seconds == 120
    assert parsed.request_timeout_seconds == 30
    assert parsed.startup_healthcheck is True
    assert parsed.allow_internet_access is True
    assert parsed.allow_public_traffic is False
    assert parsed.sandbox_user == "root"
    assert parsed.lease.ttl_seconds == 30
    assert parsed.max_cached_threads == 64


def test_e2b_sync_defaults_match_the_design() -> None:
    cfg = AppConfig.model_validate(
        {"models": _models(), "sandbox": {"e2b": _e2b()}}
    )

    sync = cfg.sandbox.e2b.sync
    assert sync.enabled is True
    assert sync.upload_uploads is True
    assert sync.upload_workspace is False
    assert sync.upload_skills is True
    assert sync.download_outputs is True
    assert sync.download_workspace is False
    assert sync.max_file_bytes == 32 * 1024 * 1024
    assert sync.max_tree_bytes == 256 * 1024 * 1024


def test_e2b_sync_rejects_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "sandbox": {"e2b": _e2b(sync={"max_file_bytes": 0})},
            }
        )
