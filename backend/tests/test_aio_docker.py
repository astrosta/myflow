"""Docker control plane for the AIO sandbox provider.

These run against an in-memory docker CLI, so they pin the exact argv the
provider would execute and the error flags the provider branches on.
"""

from __future__ import annotations

import pytest

from agentkit.sandbox.aio_sandbox.docker import (
    ContainerSpec,
    DockerClient,
    DockerContainerError,
    subprocess_runner,
)
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.host_mounts import MountEntry
from fakes.aio_docker import FakeDocker

_USER_DATA = MountEntry("/mnt/user-data", "/opt/myflow/.myflow/u1/t1/user-data", False)
_SKILLS = MountEntry("/mnt/skills/private", "/opt/myflow/cache/active/users/u1", True)


def _client(fake: FakeDocker) -> DockerClient:
    return DockerClient(runner=fake)


def _spec(**overrides) -> ContainerSpec:
    values = {
        "name": "myflow-aio-abc123",
        "image": "all-in-one-sandbox:1.11.0",
        "labels": {"myflow": "1", "myflow.user": "u1"},
        "mounts": [_USER_DATA, _SKILLS],
        "publish_host": "127.0.0.1",
        "container_port": 8080,
    }
    values.update(overrides)
    return ContainerSpec(**values)


def test_run_argv_is_explicit_and_hardened() -> None:
    argv = _spec(
        env={"TZ": "UTC"},
        cpus=2,
        memory="2g",
        pids_limit=512,
    ).run_argv()

    assert argv[:2] == ["run", "-d"]
    assert ["--name", "myflow-aio-abc123"] == argv[2:4]
    assert "--cap-drop=ALL" in argv
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # Labels are sorted so the argv is stable across runs.
    assert ["--label", "myflow=1"] == argv[argv.index("--label"):argv.index("--label") + 2]
    assert "--label" in argv and "myflow.user=u1" in argv
    assert "type=bind,source=/opt/myflow/.myflow/u1/t1/user-data,target=/mnt/user-data" in argv
    assert (
        "type=bind,source=/opt/myflow/cache/active/users/u1,target=/mnt/skills/private,readonly"
        in argv
    )
    assert argv[argv.index("-p") + 1] == "127.0.0.1::8080"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert argv[argv.index("-e") + 1] == "TZ=UTC"
    # The image is last: everything after it would be a container cmd.
    assert argv[-1] == "all-in-one-sandbox:1.11.0"


def test_network_modes() -> None:
    assert "--network" not in _spec().run_argv()
    none_argv = _spec(network_mode="none").run_argv()
    assert none_argv[none_argv.index("--network") + 1] == "none"
    named = _spec(network_name="myflow-sandbox").run_argv()
    assert named[named.index("--network") + 1] == "myflow-sandbox"


def test_run_returns_the_container_id() -> None:
    fake = FakeDocker()

    container_id = _client(fake).run(_spec())

    assert container_id == "cid001"
    assert fake.find("myflow-aio-abc123") is not None


def test_run_reports_a_name_clash() -> None:
    fake = FakeDocker()
    client = _client(fake)
    client.run(_spec())

    with pytest.raises(DockerContainerError) as exc_info:
        client.run(_spec())

    assert exc_info.value.name_in_use is True
    assert exc_info.value.not_found is False


def test_inspect_reads_id_labels_running_and_port() -> None:
    fake = FakeDocker(first_port=41000)
    client = _client(fake)
    client.run(_spec())

    state = client.inspect("myflow-aio-abc123", container_port=8080)

    assert state.container_id == "cid001"
    assert state.name == "myflow-aio-abc123"
    assert state.labels == {"myflow": "1", "myflow.user": "u1"}
    assert state.running is True
    assert state.host_port == 41000


def test_inspect_of_a_missing_container_is_not_found() -> None:
    client = _client(FakeDocker())

    with pytest.raises(DockerContainerError) as exc_info:
        client.inspect("gone", container_port=8080)

    assert exc_info.value.not_found is True


def test_stop_and_remove_tolerate_a_missing_container() -> None:
    client = _client(FakeDocker())

    client.stop("gone")
    client.remove("gone")


def test_stop_keeps_the_container_and_remove_drops_it() -> None:
    fake = FakeDocker()
    client = _client(fake)
    client.run(_spec())

    client.stop("myflow-aio-abc123")
    assert fake.find("myflow-aio-abc123").running is False

    client.start("myflow-aio-abc123")
    assert fake.find("myflow-aio-abc123").running is True

    client.remove("myflow-aio-abc123")
    assert fake.find("myflow-aio-abc123") is None


def test_list_by_labels_matches_every_label() -> None:
    fake = FakeDocker()
    client = _client(fake)
    client.run(_spec(name="one", labels={"myflow": "1", "myflow.user": "u1"}))
    client.run(_spec(name="two", labels={"myflow": "1", "myflow.user": "u2"}))

    states = client.list_by_labels({"myflow": "1", "myflow.user": "u1"})

    assert [state.name for state in states] == ["one"]


def test_list_by_labels_is_empty_without_matches() -> None:
    assert _client(FakeDocker()).list_by_labels({"myflow": "1"}) == []


def test_version_and_image_probe() -> None:
    fake = FakeDocker(version="29.9.2")
    client = _client(fake)

    assert client.version() == "29.9.2"
    assert client.image_exists("all-in-one-sandbox:1.11.0") is True

    missing = _client(FakeDocker(image_present=False))
    assert missing.image_exists("all-in-one-sandbox:1.11.0") is False


def test_bridge_gateway_is_read_from_the_daemon() -> None:
    assert _client(FakeDocker(gateway="172.17.0.1")).bridge_gateway() == "172.17.0.1"
    assert _client(FakeDocker(gateway=None)).bridge_gateway() is None


def test_a_missing_docker_cli_is_an_actionable_error() -> None:
    with pytest.raises(SandboxRuntimeError, match="docker CLI is not installed"):
        subprocess_runner(["definitely-not-a-real-docker-binary", "version"], timeout=5)
