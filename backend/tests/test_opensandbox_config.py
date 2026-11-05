"""OpenSandbox configuration surface (sandbox.opensandbox)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS
from agentkit.config.sandbox_config import (
    DEFAULT_OPENSANDBOX_IMAGE,
    HostMountConfig,
    OpenSandboxConfig,
    SandboxLeaseConfig,
)


def _models() -> list[dict]:
    return [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]


def _osb(**overrides) -> dict:
    data = {
        "host_mount": {"local_root": "/opt/myflow", "host_root": "/opt/myflow"},
        "lease": {"redis_url": "redis://127.0.0.1:6379/0"},
    }
    data.update(overrides)
    return data


def _config(**overrides) -> AppConfig:
    return AppConfig.model_validate(
        {"models": _models(), "sandbox": {"opensandbox": _osb(**overrides)}}
    )


def test_opensandbox_section_is_optional() -> None:
    assert AppConfig.model_validate({"models": _models()}).sandbox.opensandbox is None


def test_requires_host_mount_and_lease() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "sandbox": {"opensandbox": {"host_mount": {"local_root": "/x"}}},
            }
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": _models(),
                "sandbox": {"opensandbox": {"lease": {"redis_url": "redis://r/0"}}},
            }
        )


def test_defaults() -> None:
    parsed = _config().sandbox.opensandbox
    assert parsed is not None
    assert parsed.domain == "127.0.0.1:8080"
    assert parsed.protocol == "http"
    assert parsed.api_key == ""
    assert parsed.endpoint_host == ""
    assert parsed.endpoint_host_map == {}
    assert parsed.use_server_proxy is False
    assert parsed.image == DEFAULT_OPENSANDBOX_IMAGE == "python:3.12"
    assert parsed.timeout_seconds == 1800
    assert parsed.ready_timeout_seconds == 30
    assert parsed.request_timeout_seconds == 30
    assert parsed.acquire_timeout_seconds == 120
    assert parsed.resource == {"cpu": "2", "memory": "2Gi"}
    assert parsed.env == {}
    assert parsed.release_action == "keep"
    assert parsed.max_cached_threads == 64
    assert parsed.discover_existing is True
    assert parsed.startup_healthcheck is True
    assert parsed.prewarm is True
    assert parsed.prewarm_max_concurrent == 1
    assert parsed.orphan_policy == "report"
    assert parsed.orphan_ttl_seconds == 86400


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _config(unknown="x")


def test_section_is_startup_only() -> None:
    assert "sandbox.opensandbox" in STARTUP_ONLY_FIELDS


def test_release_action_destroy_is_allowed() -> None:
    parsed = _config(release_action="destroy").sandbox.opensandbox
    assert parsed is not None
    assert parsed.release_action == "destroy"


def test_blank_domain_or_image_is_rejected() -> None:
    for field in ("domain", "image"):
        with pytest.raises(ValidationError):
            _config(**{field: "   "})


def test_env_keys_must_be_posix_names() -> None:
    parsed = _config(env={"MYFLOW_TOKEN": "t"}).sandbox.opensandbox
    assert parsed is not None
    assert parsed.env == {"MYFLOW_TOKEN": "t"}

    with pytest.raises(ValidationError):
        _config(env={"1BAD": "t"})


def test_protocol_is_limited() -> None:
    _config(protocol="https")
    with pytest.raises(ValidationError):
        _config(protocol="ftp")


def test_resource_and_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        _config(ready_timeout_seconds=0)
    with pytest.raises(ValidationError):
        _config(max_cached_threads=0)
    with pytest.raises(ValidationError):
        _config(timeout_seconds=0)


def test_endpoint_rewrite_for_host_and_container_deployments() -> None:
    host = _config(endpoint_host="127.0.0.1").sandbox.opensandbox
    assert host is not None
    assert host.endpoint_host == "127.0.0.1"

    container = _config(
        endpoint_host_map={"host.docker.internal": "host.docker.internal"}
    ).sandbox.opensandbox
    assert container is not None
    assert container.endpoint_host_map == {"host.docker.internal": "host.docker.internal"}


def test_endpoint_rewrite_rejects_urls_and_host_ports() -> None:
    for value in ("http://127.0.0.1", "127.0.0.1/proxy", "127.0.0.1:44772"):
        with pytest.raises(ValidationError):
            _config(endpoint_host=value)

    with pytest.raises(ValidationError):
        _config(endpoint_host_map={"http://host.docker.internal": "127.0.0.1"})
    with pytest.raises(ValidationError):
        _config(endpoint_host_map={"host.docker.internal": "127.0.0.1:44772"})
    with pytest.raises(ValidationError):
        _config(endpoint_host_map={"host.docker.internal": ""})


def test_endpoint_host_map_keys_may_include_a_port() -> None:
    parsed = _config(
        endpoint_host_map={"host.docker.internal:44772": "127.0.0.1"}
    ).sandbox.opensandbox
    assert parsed is not None
    assert parsed.endpoint_host_map == {"host.docker.internal:44772": "127.0.0.1"}


def test_use_server_proxy_is_opt_in() -> None:
    parsed = _config(use_server_proxy=True).sandbox.opensandbox
    assert parsed is not None
    assert parsed.use_server_proxy is True


def test_model_builds_directly() -> None:
    config = OpenSandboxConfig(
        host_mount=HostMountConfig(local_root="/opt/myflow"),
        lease=SandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    assert config.image == "python:3.12"
    assert config.host_mount.host_root is None
