"""AIO sandbox configuration surface (sandbox.aio)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS
from agentkit.config.sandbox_config import (
    DEFAULT_AIO_IMAGE,
    AioSandboxConfig,
    HostMountConfig,
    SandboxLeaseConfig,
)


def _models() -> list[dict]:
    return [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]


def _aio(**overrides) -> dict:
    data = {
        "host_mount": {"local_root": "/opt/myflow", "host_root": "/opt/myflow"},
        "lease": {"redis_url": "redis://127.0.0.1:6379/0"},
    }
    data.update(overrides)
    return data


def _config(**overrides) -> AppConfig:
    return AppConfig.model_validate(
        {"models": _models(), "sandbox": {"aio": _aio(**overrides)}}
    )


def test_aio_section_is_optional() -> None:
    assert AppConfig.model_validate({"models": _models()}).sandbox.aio is None


def test_aio_requires_host_mount_and_lease() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"aio": {"host_mount": {"local_root": "/x"}}}}
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"models": _models(), "sandbox": {"aio": {"lease": {"redis_url": "redis://r/0"}}}}
        )


def test_aio_defaults() -> None:
    parsed = _config().sandbox.aio
    assert parsed is not None
    assert parsed.image == DEFAULT_AIO_IMAGE
    assert parsed.image.endswith("all-in-one-sandbox:1.11.0")
    assert parsed.pull_policy == "missing"
    assert parsed.container_name_prefix == "myflow-aio"
    assert parsed.port == 8080
    assert parsed.publish_host == "auto"
    assert parsed.connect_host == "auto"
    assert parsed.ready_timeout_seconds == 60
    assert parsed.request_timeout_seconds == 30
    assert parsed.release_action == "stop"
    assert parsed.max_cached_threads == 64
    assert parsed.discover_existing is True
    assert parsed.startup_healthcheck is True
    assert parsed.env == {}
    assert parsed.network.mode == "bridge"
    assert parsed.network.name is None
    assert parsed.resources.cpus is None
    assert parsed.resources.memory is None
    assert parsed.resources.pids_limit is None
    assert parsed.orphan_policy == "report"
    assert parsed.orphan_ttl_seconds == 86400


def test_aio_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _config(unknown="x")
    with pytest.raises(ValidationError):
        _config(resources={"disk": "10g"})


def test_aio_section_is_startup_only() -> None:
    assert "sandbox.aio" in STARTUP_ONLY_FIELDS


def test_host_mount_and_lease_models_are_shared_with_cube() -> None:
    from agentkit.config.sandbox_config import (
        CubeSandboxHostMountConfig,
        CubeSandboxLeaseConfig,
    )

    assert CubeSandboxHostMountConfig is HostMountConfig
    assert CubeSandboxLeaseConfig is SandboxLeaseConfig


def test_release_action_accepts_remove() -> None:
    parsed = _config(release_action="remove").sandbox.aio
    assert parsed is not None
    assert parsed.release_action == "remove"


def test_container_name_prefix_must_be_a_valid_docker_name() -> None:
    with pytest.raises(ValidationError):
        _config(container_name_prefix="bad name")
    with pytest.raises(ValidationError):
        _config(container_name_prefix="-leading-dash")


def test_blank_image_or_hosts_are_rejected() -> None:
    for field in ("image", "publish_host", "connect_host"):
        with pytest.raises(ValidationError):
            _config(**{field: "  "})


def test_env_keys_must_be_posix_names() -> None:
    parsed = _config(env={"MYFLOW_TOKEN": "t"}).sandbox.aio
    assert parsed is not None
    assert parsed.env == {"MYFLOW_TOKEN": "t"}

    with pytest.raises(ValidationError):
        _config(env={"1BAD": "t"})


def test_resource_and_timeout_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        _config(ready_timeout_seconds=0)
    with pytest.raises(ValidationError):
        _config(max_cached_threads=0)
    with pytest.raises(ValidationError):
        _config(resources={"pids_limit": 0})
    with pytest.raises(ValidationError):
        _config(port=70000)


def test_network_name_is_accepted_for_a_custom_bridge() -> None:
    parsed = _config(network={"mode": "none", "name": "myflow-sandbox"}).sandbox.aio
    assert parsed is not None
    assert parsed.network.mode == "none"
    assert parsed.network.name == "myflow-sandbox"


def test_aio_model_can_be_built_directly() -> None:
    config = AioSandboxConfig(
        host_mount=HostMountConfig(local_root="/opt/myflow"),
        lease=SandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    assert config.image == DEFAULT_AIO_IMAGE
    assert config.host_mount.host_root is None
