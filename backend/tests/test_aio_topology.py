"""AIO sandbox topology: where the port is published and how the Gateway dials it.

A Gateway on the host and a Gateway in a container with the docker socket need
different answers, and getting either wrong produces a container that looks
healthy but never answers. These tests pin the resolution rules.
"""

from __future__ import annotations

import pytest

from agentkit.config.sandbox_config import AioSandboxConfig, HostMountConfig, SandboxLeaseConfig
from agentkit.sandbox.aio_sandbox.docker import (
    detect_in_container,
    resolve_topology,
)


def _config(**overrides) -> AioSandboxConfig:
    data = {
        "host_mount": HostMountConfig(local_root="/opt/myflow"),
        "lease": SandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    }
    data.update(overrides)
    return AioSandboxConfig(**data)


def test_host_gateway_uses_loopback(monkeypatch) -> None:
    monkeypatch.delenv("MYFLOW_IN_CONTAINER", raising=False)

    topology = resolve_topology(_config(), in_container=False)

    assert topology.in_container is False
    assert topology.publish_host == "127.0.0.1"
    assert topology.connect_host == "127.0.0.1"


def test_container_gateway_uses_the_bridge_gateway_for_both_ends() -> None:
    topology = resolve_topology(
        _config(), in_container=True, gateway_lookup=lambda: "172.17.0.1"
    )

    assert topology.in_container is True
    assert topology.publish_host == "172.17.0.1"
    assert topology.connect_host == "172.17.0.1"


def test_container_gateway_without_a_gateway_fails_loudly() -> None:
    from agentkit.sandbox.exceptions import SandboxRuntimeError

    with pytest.raises(SandboxRuntimeError, match="bridge gateway"):
        resolve_topology(_config(), in_container=True, gateway_lookup=lambda: None)

    with pytest.raises(SandboxRuntimeError, match="bridge gateway"):
        resolve_topology(_config(), in_container=True)


def test_both_explicit_values_are_used_verbatim() -> None:
    topology = resolve_topology(
        _config(publish_host="192.168.1.10", connect_host="192.168.1.11"),
        in_container=True,
        gateway_lookup=lambda: None,
    )

    assert (topology.publish_host, topology.connect_host) == (
        "192.168.1.10",
        "192.168.1.11",
    )


def test_explicit_publish_host_is_mirrored_by_auto_connect() -> None:
    topology = resolve_topology(
        _config(publish_host="172.17.0.1"), in_container=True, gateway_lookup=None
    )

    assert (topology.publish_host, topology.connect_host) == ("172.17.0.1", "172.17.0.1")


def test_explicit_connect_host_is_mirrored_by_auto_publish() -> None:
    topology = resolve_topology(
        _config(connect_host="host.docker.internal"), in_container=True
    )

    assert (topology.publish_host, topology.connect_host) == (
        "host.docker.internal",
        "host.docker.internal",
    )


def test_explicit_values_survive_a_host_gateway() -> None:
    topology = resolve_topology(
        _config(connect_host="10.0.0.5"), in_container=False, gateway_lookup=None
    )

    assert topology.publish_host == "10.0.0.5"
    assert topology.connect_host == "10.0.0.5"


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_in_container_env_override_is_true(value: str, monkeypatch) -> None:
    monkeypatch.setenv("MYFLOW_IN_CONTAINER", value)
    assert detect_in_container() is True


@pytest.mark.parametrize("value", ["0", "false", "No", "off"])
def test_in_container_env_override_is_false(value: str, monkeypatch) -> None:
    monkeypatch.setenv("MYFLOW_IN_CONTAINER", value)
    assert detect_in_container() is False


def test_detect_without_override_reports_a_boolean(monkeypatch) -> None:
    monkeypatch.delenv("MYFLOW_IN_CONTAINER", raising=False)
    assert isinstance(detect_in_container(), bool)
