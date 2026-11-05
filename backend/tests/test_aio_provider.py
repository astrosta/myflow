"""AioSandboxProviderDocker lifecycle over a fake docker CLI.

The fake records the container spec, so these tests can assert what the provider
actually asks Docker for, and they drive the client through the host mount map,
so the acquire canary runs for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    AioSandboxConfig,
    HostMountConfig,
    SandboxLeaseConfig,
)
from agentkit.sandbox.aio_sandbox.client import AioTransportError
from agentkit.sandbox.aio_sandbox.docker import DockerClient
from agentkit.sandbox.aio_sandbox.provider import AioSandboxProviderDocker
from agentkit.sandbox.exceptions import SandboxRuntimeError
from fakes.aio_client import HostMountedAioClient
from fakes.aio_docker import FakeDocker

_USER_DATA = "/mnt/user-data"


class FakeLock:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []
        self.pings = 0
        self.renew_calls = 0

    def ping(self) -> bool:
        self.pings += 1
        return True

    def acquire(self, key: str, owner: str, timeout: float) -> None:
        del key, owner, timeout

    def renew(self, key: str, owner: str) -> bool:
        del key, owner
        self.renew_calls += 1
        return True

    def release(self, key: str, owner: str) -> None:
        self.released.append((key, owner))


class DownLock(FakeLock):
    def ping(self) -> bool:
        return False


def _config(tmp_path: Path, **overrides) -> AppConfig:
    data = {
        "host_mount": {"local_root": str(tmp_path), "host_root": str(tmp_path)},
        "lease": {"redis_url": "redis://127.0.0.1:6379/0"},
    }
    data.update(overrides)
    skills = SkillsConfig(
        source="git",
        path=str(tmp_path / "skills"),
        git={"cache_dir": str(tmp_path / "cache")},
        builtin={"path": str(tmp_path / "skills" / "builtin")},
    )
    (tmp_path / "skills" / "builtin").mkdir(parents=True, exist_ok=True)
    return AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(aio=AioSandboxConfig(**data)),
        skills=skills,
    )


def _provider(
    tmp_path: Path,
    *,
    config: AppConfig | None = None,
    lock=None,
    docker=None,
    docker_client=None,
):
    active_config = config if config is not None else _config(tmp_path)
    fake_docker = docker if docker is not None else FakeDocker()
    clients: dict[str, HostMountedAioClient] = {}

    def factory(base_url: str) -> HostMountedAioClient:
        port = int(base_url.rsplit(":", 1)[1])
        container = next(
            (row for row in fake_docker.containers.values() if row.host_port == port),
            None,
        )
        root_map: dict[str, str] = {}
        if container is not None:
            for mount in container.mounts:
                parts = dict(item.split("=", 1) for item in mount.split(",") if "=" in item)
                if "source" in parts and "target" in parts:
                    root_map[parts["target"]] = parts["source"]
        client = HostMountedAioClient(root_map)
        clients[base_url] = client
        return client

    provider = AioSandboxProviderDocker(
        app_config=active_config,
        docker_client=(
            docker_client
            if docker_client is not None
            else DockerClient(runner=fake_docker)
        ),
        client_factory=factory,
        lock=lock if lock is not None else FakeLock(),
        paths=Paths(tmp_path / ".myflow"),
        instance_id="gw-test",
    )
    return provider, fake_docker, clients


def _thread_dir(tmp_path: Path, thread: str, user: str = "u1") -> Path:
    return Paths(tmp_path / ".myflow").sandbox_work_dir(thread, user_id=user)


# -- acquire ----------------------------------------------------------------


def test_acquire_creates_a_labelled_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)

    sandbox_id = provider.acquire("t1", user_id="u1")

    assert sandbox_id == "aio:u1:t1"
    assert len(docker.containers) == 1
    container = next(iter(docker.containers.values()))
    assert container.labels["myflow"] == "1"
    assert container.labels["myflow.provider"] == "aio"
    assert container.labels["myflow.user"] == "u1"
    assert container.labels["myflow.thread"] == "t1"
    assert container.labels["myflow.instance"] == "gw-test"
    assert container.labels["myflow.mounts_hash"]
    assert container.image.endswith("all-in-one-sandbox:1.11.0")
    assert container.publish_host == "127.0.0.1"


def test_acquire_mounts_the_thread_workspace(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)

    provider.acquire("t1", user_id="u1")

    container = next(iter(docker.containers.values()))
    planned = provider._build_mounts("u1", "t1")
    user_data = next(entry for entry in planned if entry.container_path == _USER_DATA)
    assert (
        "type=bind,source=" + user_data.host_path + ",target=" + _USER_DATA
        in container.mounts
    )
    # The skills tree is mounted read-only, same as the CubeSandbox provider.
    assert any(
        mount.endswith("target=/mnt/skills/private,readonly") for mount in container.mounts
    )


def test_acquire_runs_the_canary_and_cleans_it_up(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path)

    provider.acquire("t1", user_id="u1")

    canary = _thread_dir(tmp_path, "t1") / ".myflow-canary"
    assert not canary.exists()


def test_canary_failure_removes_the_container(tmp_path) -> None:
    provider, docker, clients = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    provider.release(provider.acquire("t1", user_id="u1"))
    docker.containers.clear()

    # A container whose mount does not contain the canary file must be rejected.
    class BrokenClient(HostMountedAioClient):
        def read_text(self, path: str, *, start_line=None, end_line=None) -> str:
            return "not-the-token"

    _build_factory = provider._client_factory
    provider._client_factory = lambda base_url: BrokenClient({})

    with pytest.raises(SandboxRuntimeError, match="canary"):
        provider.acquire("t2", user_id="u1")

    assert docker.containers == {}
    provider._client_factory = _build_factory


def test_second_acquire_reuses_the_same_handle(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)

    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t1", user_id="u1")

    assert first == second
    assert len(docker.containers) == 1
    assert len([call for call in docker.calls if call[1] == "run"]) == 1


def test_acquire_restarts_a_stopped_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    container.running = False

    assert provider.acquire("t1", user_id="u1") == sandbox_id
    assert container.running is True
    assert len(docker.containers) == 1


def test_acquire_recreates_a_removed_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    docker.containers.clear()

    assert provider.acquire("t1", user_id="u1") == sandbox_id
    assert len(docker.containers) == 1


def test_acquire_adopts_a_container_from_another_instance(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    container.labels["myflow.instance"] = "other-gateway"
    provider.reset()

    provider.acquire("t1", user_id="u1")

    assert len(docker.containers) == 1
    assert container.labels["myflow.instance"] == "other-gateway"


def test_stale_mounts_recreate_the_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    container.labels["myflow.mounts_hash"] = "stale"
    provider.reset()

    provider.acquire("t1", user_id="u1")

    assert len(docker.containers) == 1
    fresh = next(iter(docker.containers.values()))
    assert fresh.labels["myflow.mounts_hash"] != "stale"


# -- release / reset / shutdown --------------------------------------------


def test_release_stops_the_container_and_frees_the_lease(tmp_path) -> None:
    lock = FakeLock()
    provider, docker, _ = _provider(tmp_path, lock=lock)
    sandbox_id = provider.acquire("t1", user_id="u1")

    provider.release(sandbox_id)

    container = next(iter(docker.containers.values()))
    assert container.running is False
    assert provider.get(sandbox_id) is None
    assert [key for key, _ in lock.released] == ["myflow:sandbox:u1:t1"]


def test_reacquire_after_release_reuses_the_same_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    provider.release(sandbox_id)

    provider.acquire("t1", user_id="u1")

    assert len(docker.containers) == 1
    assert next(iter(docker.containers.values())).container_id == container.container_id
    assert container.running is True


def test_release_action_remove_deletes_the_container(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path, config=_config(tmp_path, release_action="remove"))
    sandbox_id = provider.acquire("t1", user_id="u1")

    provider.release(sandbox_id)

    assert docker.containers == {}


def test_reset_keeps_the_container_running(tmp_path) -> None:
    lock = FakeLock()
    provider, docker, _ = _provider(tmp_path, lock=lock)
    provider.acquire("t1", user_id="u1")

    provider.reset()

    assert next(iter(docker.containers.values())).running is True
    assert lock.released


def test_shutdown_removes_every_container(tmp_path) -> None:
    lock = FakeLock()
    provider, docker, _ = _provider(tmp_path, lock=lock)
    provider.acquire("t1", user_id="u1")
    provider.acquire("t2", user_id="u1")

    provider.shutdown()

    assert docker.containers == {}
    assert len(lock.released) == 2


def test_get_is_memory_only(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    docker.calls.clear()

    assert provider.get(sandbox_id) is not None
    assert docker.calls == []
    assert provider.get("local:u1:t1") is None
    assert provider.get("aio:") is None


def test_release_of_a_foreign_id_does_nothing(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    docker.calls.clear()

    provider.release("local:u1:t1")

    assert docker.calls == []


# -- startup checks ---------------------------------------------------------


def test_startup_check_pings_docker_topology_and_redis(tmp_path) -> None:
    lock = FakeLock()
    provider, _, _ = _provider(tmp_path, lock=lock)

    provider.startup_check()

    assert lock.pings == 1


def test_startup_check_fails_without_redis(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path, lock=DownLock())

    with pytest.raises(SandboxRuntimeError, match="Redis PING failed"):
        provider.startup_check()


def test_startup_check_fails_when_docker_is_unreachable(tmp_path) -> None:
    class BrokenDockerClient(DockerClient):
        def version(self) -> str:
            raise SandboxRuntimeError("cannot connect to the docker daemon")

    provider, _, _ = _provider(
        tmp_path, docker_client=BrokenDockerClient(runner=FakeDocker())
    )

    with pytest.raises(SandboxRuntimeError, match="docker daemon is not reachable"):
        provider.startup_check()


def test_startup_check_reports_foreign_containers_without_removing_them(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    container.labels["myflow.instance"] = "other-gateway"
    provider.reset()

    provider.startup_check()

    assert len(docker.containers) == 1


def test_startup_check_can_remove_stale_orphans(tmp_path) -> None:
    config = _config(tmp_path, orphan_policy="remove", orphan_ttl_seconds=0)
    provider, docker, _ = _provider(tmp_path, config=config)
    provider.acquire("t1", user_id="u1")
    container = next(iter(docker.containers.values()))
    container.labels["myflow.instance"] = "other-gateway"
    container.labels["myflow.created_at"] = "1"
    provider.reset()

    provider.startup_check()

    assert docker.containers == {}


# -- capability flags and identity ------------------------------------------


def test_capability_flags(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path)

    assert provider.uses_thread_data_mounts is True
    assert provider.supports_agent_skill_isolation is True
    assert provider.supports_per_user_skill_mounts is True


def test_container_name_is_stable_and_distinct(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    provider.acquire("t2", user_id="u1")
    provider.acquire("t1", user_id="u2")

    names = set(docker.containers)
    assert len(names) == 3
    assert all(name.startswith("myflow-aio-") for name in names)


def test_generic_thread_uses_the_sentinel(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)

    sandbox_id = provider.acquire(None, user_id="u1")

    assert sandbox_id == "aio:u1:__generic__"
    container = next(iter(docker.containers.values()))
    assert container.labels["myflow.thread"] == "__generic__"


def test_mounts_hash_matches_the_planned_mounts(tmp_path) -> None:
    provider, docker, _ = _provider(tmp_path)
    expected = provider.desired_mounts_hash("u1", "t1")

    provider.acquire("t1", user_id="u1")

    container = next(iter(docker.containers.values()))
    assert container.labels["myflow.mounts_hash"] == expected


# -- dead containers --------------------------------------------------------


def test_transport_failure_drops_a_removed_container(tmp_path) -> None:
    provider, docker, clients = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    client = next(iter(clients.values()))
    docker.containers.clear()

    # The sandbox reports the transport failure; the provider probes and drops.
    client.command_error = AioTransportError("connection refused")
    with pytest.raises(Exception):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is None


def test_transport_failure_keeps_a_running_container(tmp_path) -> None:
    provider, docker, clients = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    client = next(iter(clients.values()))

    client.command_error = AioTransportError("connection reset")
    with pytest.raises(Exception):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is sandbox
    assert len(docker.containers) == 1


# -- LRU --------------------------------------------------------------------


def test_lru_eviction_stops_but_keeps_the_container(tmp_path) -> None:
    config = _config(tmp_path, max_cached_threads=1)
    provider, docker, _ = _provider(tmp_path, config=config)
    provider.acquire("t1", user_id="u1")
    first = next(iter(docker.containers.values()))

    provider.acquire("t2", user_id="u1")

    assert len(docker.containers) == 2
    assert first.running is False


def test_sandbox_id_round_trip(tmp_path) -> None:
    provider, _, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")

    assert provider.get("aio:u1:t1") is not None
    assert provider.get("cube:u1:t1") is None
