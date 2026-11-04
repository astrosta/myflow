from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS


def _models() -> list[dict]:
    return [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]


def _cube(**overrides) -> dict:
    data = {
        "api_url": "http://127.0.0.1:3000",
        "template_id": "tpl-1",
        "host_mount": {"local_root": "/opt/myflow", "host_root": "/opt/myflow"},
        "lease": {"redis_url": "redis://127.0.0.1:6379/0", "ttl_seconds": 30},
    }
    data.update(overrides)
    return data


def test_local_config_does_not_require_cube_section() -> None:
    cfg = AppConfig.model_validate({"models": _models()})
    assert cfg.sandbox.cube is None


def test_cube_config_requires_template_and_lease() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"cube": {"host_mount": {"local_root": "/x", "host_root": "/x"}}}}
        )


def test_cube_host_root_may_default_to_local_root() -> None:
    cube = _cube()
    cube["host_mount"] = {"local_root": "/opt/myflow"}
    cfg = AppConfig.model_validate({"models": _models(), "sandbox": {"cube": cube}})
    assert cfg.sandbox.cube is not None
    assert cfg.sandbox.cube.host_mount.host_root is None


def test_cube_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"cube": _cube(unknown="x")}}
        )


def test_cube_section_is_startup_only() -> None:
    assert "sandbox.cube" in STARTUP_ONLY_FIELDS


def test_cube_config_models_are_exported() -> None:
    from agentkit.config import (
        CubeSandboxConfig,
        CubeSandboxHostMountConfig,
        CubeSandboxLeaseConfig,
        CubeSandboxNetworkConfig,
    )

    assert CubeSandboxConfig is not None
    assert CubeSandboxHostMountConfig is not None
    assert CubeSandboxLeaseConfig is not None
    assert CubeSandboxNetworkConfig is not None


def test_cube_config_documented_defaults() -> None:
    cube = _cube()
    cube["host_mount"] = {"local_root": "/opt/myflow"}
    cube["lease"] = {"redis_url": "redis://127.0.0.1:6379/0"}
    cfg = AppConfig.model_validate({"models": _models(), "sandbox": {"cube": cube}})

    parsed = cfg.sandbox.cube
    assert parsed is not None
    assert parsed.api_url == "http://127.0.0.1:3000"
    assert parsed.sandbox_domain == "cube.app"
    assert parsed.proxy_port == 80
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
    assert parsed.host_mount.host_root is None
    assert parsed.host_mount.allowed_prefix is None
    assert parsed.lease.ttl_seconds == 30
    assert parsed.max_cached_threads == 64
