"""OpenSandboxProvider lifecycle over a fake server client.

The fake handle reads and writes through the mount map, so the acquire canary
runs for real. Everything else here pins the provider's decisions: what it asks
the server for, what it keeps in metadata, and when it drops or kills a sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    HostMountConfig,
    OpenSandboxConfig,
    SandboxLeaseConfig,
)
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.opensandbox.client import OsbTransportError
from agentkit.sandbox.opensandbox.provider import OpenSandboxProvider
from fakes.osb_client import FakeOsbClient

_USER_DATA = "/mnt/user-data"


class FakeLock:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        return True

    def acquire(self, key: str, owner: str, timeout: float) -> None:
        del key, owner, timeout

    def renew(self, key: str, owner: str) -> bool:
        del key, owner
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
        sandbox=SandboxConfig(opensandbox=OpenSandboxConfig(**data)),
        skills=skills,
    )


def _provider(tmp_path: Path, *, config: AppConfig | None = None, lock=None, client=None):
    active_config = config if config is not None else _config(tmp_path)
    fake_client = client if client is not None else FakeOsbClient()
    provider = OpenSandboxProvider(
        app_config=active_config,
        client=fake_client,
        lock=lock if lock is not None else FakeLock(),
        paths=Paths(tmp_path / ".myflow"),
        instance_id="gw-test",
    )
    return provider, fake_client


def _workspace(tmp_path: Path, thread: str, user: str = "u1") -> Path:
    return Paths(tmp_path / ".myflow").sandbox_work_dir(thread, user_id=user)


# -- acquire ----------------------------------------------------------------


def test_acquire_creates_a_sandbox_with_metadata_and_mounts(tmp_path) -> None:
    provider, client = _provider(tmp_path)

    sandbox_id = provider.acquire("t1", user_id="u1")

    assert sandbox_id == "osb:u1:t1"
    assert len(client.create_calls) == 1
    call = client.create_calls[0]
    assert call["image"] == "python:3.12"
    assert call["timeout_seconds"] == 1800
    assert call["ready_timeout_seconds"] == 30
    assert call["resource"] == {"cpu": "2", "memory": "2Gi"}
    assert call["metadata"]["myflow"] == "1"
    assert call["metadata"]["myflow.user"] == "u1"
    assert call["metadata"]["myflow.thread"] == "t1"
    assert call["metadata"]["myflow.instance"] == "gw-test"
    assert call["metadata"]["myflow.mounts_hash"]
    mounted = {entry.container_path: entry.host_path for entry in call["mounts"]}
    assert _USER_DATA in mounted
    assert "/mnt/skills/private" in mounted


def test_acquire_runs_the_canary_and_cleans_it_up(tmp_path) -> None:
    provider, _ = _provider(tmp_path)

    provider.acquire("t1", user_id="u1")

    assert not (_workspace(tmp_path, "t1") / ".myflow-canary").exists()


def test_canary_failure_closes_the_handle_and_raises(tmp_path) -> None:
    provider, client = _provider(tmp_path)

    class BrokenHandle(type(client.created)):  # placeholder, replaced below
        pass

    real_create = client.create

    def create(**kwargs):
        handle = real_create(**kwargs)
        handle.root_map = {}  # nothing is mounted: the canary must fail

        def broken_read(path: str) -> str:
            raise FileNotFoundError(path)

        handle.read_text = broken_read  # type: ignore[assignment]
        return handle

    client.create = create  # type: ignore[assignment]

    with pytest.raises(SandboxRuntimeError, match="canary"):
        provider.acquire("t1", user_id="u1")

    assert provider.get("osb:u1:t1") is None


def test_second_acquire_reuses_the_same_sandbox(tmp_path) -> None:
    provider, client = _provider(tmp_path)

    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t1", user_id="u1")

    assert first == second
    assert len(client.create_calls) == 1


def test_acquire_recreates_when_the_sandbox_is_gone(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    client.created[0].running = False

    assert provider.acquire("t1", user_id="u1") == sandbox_id
    assert len(client.create_calls) == 2


def test_acquire_adopts_a_sandbox_from_another_instance(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    created = client.created[0]
    fingerprint = client.create_calls[0]["metadata"]["myflow.mounts_hash"]
    client.existing.append(
        client.summary(
            "sbx-remote",
            {
                "myflow": "1",
                "myflow.user": "u1",
                "myflow.thread": "t1",
                "myflow.instance": "other-gateway",
                "myflow.mounts_hash": fingerprint,
                "myflow.created_at": "1",
            },
        )
    )
    client.existing = [row for row in client.existing if row.sandbox_id != created.sandbox_id]
    client.created = []
    provider.reset()

    provider.acquire("t1", user_id="u1")

    assert client.connects == ["sbx-remote"]
    assert client.create_calls and len(client.create_calls) == 1


def test_stale_mounts_kill_and_recreate(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    client.existing.append(
        client.summary(
            "sbx-stale",
            {
                "myflow": "1",
                "myflow.user": "u1",
                "myflow.thread": "t1",
                "myflow.instance": "other-gateway",
                "myflow.mounts_hash": "stale",
                "myflow.created_at": "1",
            },
        )
    )

    provider.acquire("t1", user_id="u1")

    assert "sbx-stale" in client.killed
    assert len(client.create_calls) == 1


# -- release / reset / shutdown --------------------------------------------


def test_release_keep_leaves_the_sandbox_and_frees_the_lease(tmp_path) -> None:
    lock = FakeLock()
    provider, client = _provider(tmp_path, lock=lock)
    sandbox_id = provider.acquire("t1", user_id="u1")

    provider.release(sandbox_id)

    assert client.killed == []
    assert provider.get(sandbox_id) is None
    assert [key for key, _ in lock.released] == ["myflow:sandbox:u1:t1"]
    assert client.created[0].closed is True


def test_release_destroy_kills_the_sandbox(tmp_path) -> None:
    provider, client = _provider(
        tmp_path, config=_config(tmp_path, release_action="destroy")
    )
    sandbox_id = provider.acquire("t1", user_id="u1")

    provider.release(sandbox_id)

    assert client.killed == ["sbx-1"]


def test_reacquire_after_release_connects_instead_of_creating(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    fingerprint = client.create_calls[0]["metadata"]["myflow.mounts_hash"]
    provider.release(sandbox_id)
    client.summary(
        "sbx-1",
        {
            "myflow": "1",
            "myflow.user": "u1",
            "myflow.thread": "t1",
            "myflow.instance": "gw-test",
            "myflow.mounts_hash": fingerprint,
            "myflow.created_at": "1",
        },
    )

    provider.acquire("t1", user_id="u1")

    assert len(client.create_calls) == 1
    assert client.connects


def test_reset_keeps_the_remote_sandbox(tmp_path) -> None:
    lock = FakeLock()
    provider, client = _provider(tmp_path, lock=lock)
    provider.acquire("t1", user_id="u1")

    provider.reset()

    assert client.killed == []
    assert lock.released


def test_shutdown_kills_every_sandbox(tmp_path) -> None:
    lock = FakeLock()
    provider, client = _provider(tmp_path, lock=lock)
    provider.acquire("t1", user_id="u1")
    provider.acquire("t2", user_id="u1")

    provider.shutdown()

    assert sorted(client.killed) == ["sbx-1", "sbx-2"]
    assert len(lock.released) == 2
    assert client.closed is True


def test_get_is_memory_only(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    client.list_calls.clear()

    assert provider.get(sandbox_id) is not None
    assert client.list_calls == []
    assert provider.get("aio:u1:t1") is None
    assert provider.get("osb:") is None


def test_release_of_a_foreign_id_does_nothing(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    client.killed.clear()

    provider.release("local:u1:t1")

    assert client.killed == []


# -- startup checks ---------------------------------------------------------


def test_startup_check_pings_the_server_and_redis(tmp_path) -> None:
    lock = FakeLock()
    provider, client = _provider(tmp_path, lock=lock)

    provider.startup_check()

    assert client.ping_calls == 1
    assert lock.pings == 1


def test_startup_check_fails_without_the_server(tmp_path) -> None:
    client = FakeOsbClient(ping_error=RuntimeError("connection refused"))
    provider, _ = _provider(tmp_path, client=client)

    with pytest.raises(SandboxRuntimeError, match="not reachable"):
        provider.startup_check()


def test_startup_check_fails_without_redis(tmp_path) -> None:
    provider, _ = _provider(tmp_path, lock=DownLock())

    with pytest.raises(SandboxRuntimeError, match="Redis PING failed"):
        provider.startup_check()


def test_startup_check_reports_foreign_sandboxes_without_killing_them(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    client.summary(
        "sbx-foreign",
        {"myflow": "1", "myflow.instance": "other-gateway"},
    )

    provider.startup_check()

    assert client.killed == []


def test_startup_check_can_remove_stale_orphans(tmp_path) -> None:
    config = _config(tmp_path, orphan_policy="remove", orphan_ttl_seconds=0)
    provider, client = _provider(tmp_path, config=config)
    client.summary(
        "sbx-foreign",
        {"myflow": "1", "myflow.instance": "other-gateway", "myflow.created_at": "1"},
    )

    provider.startup_check()

    assert client.killed == ["sbx-foreign"]


# -- capability flags and identity ------------------------------------------


def test_capability_flags(tmp_path) -> None:
    provider, _ = _provider(tmp_path)

    assert provider.uses_thread_data_mounts is True
    assert provider.supports_agent_skill_isolation is True
    assert provider.supports_per_user_skill_mounts is True


def test_generic_thread_uses_the_sentinel(tmp_path) -> None:
    provider, client = _provider(tmp_path)

    sandbox_id = provider.acquire(None, user_id="u1")

    assert sandbox_id == "osb:u1:__generic__"
    assert client.create_calls[0]["metadata"]["myflow.thread"] == "__generic__"


def test_mounts_hash_matches_the_planned_mounts(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    expected = provider.desired_mounts_hash("u1", "t1")

    provider.acquire("t1", user_id="u1")

    assert client.create_calls[0]["metadata"]["myflow.mounts_hash"] == expected


# -- dead sandboxes ---------------------------------------------------------


def test_transport_failure_drops_a_gone_sandbox(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None

    # The sandbox is gone from the server, and the next call cannot reach it.
    client.created[0].running = False
    client.connect_error = None
    handle = client.created[0]
    handle.command_error = OsbTransportError("server unreachable")

    with pytest.raises(Exception):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is None


def test_transport_failure_keeps_a_running_sandbox(tmp_path) -> None:
    provider, client = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    client.created[0].command_error = OsbTransportError("transient")

    with pytest.raises(Exception):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is sandbox
    assert client.killed == []


# -- LRU --------------------------------------------------------------------


def test_lru_eviction_drops_the_handle_but_keeps_the_sandbox(tmp_path) -> None:
    config = _config(tmp_path, max_cached_threads=1)
    provider, client = _provider(tmp_path, config=config)
    provider.acquire("t1", user_id="u1")

    provider.acquire("t2", user_id="u1")

    assert client.killed == []
    assert client.created[0].closed is True
    assert provider.get("osb:u1:t1") is None
