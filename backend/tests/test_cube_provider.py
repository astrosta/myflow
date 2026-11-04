import asyncio
import json
import time

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    CubeSandboxConfig,
    CubeSandboxHostMountConfig,
    CubeSandboxLeaseConfig,
)
from agentkit.sandbox.cube.provider import CubeSandboxProvider
from agentkit.sandbox.exceptions import SandboxNotFoundError, SandboxRuntimeError
from fakes.cube_sdk import FakeSdkFactory


class FakeLock:
    def __init__(self): self.released = []; self.pings = 0
    def ping(self): self.pings += 1
    def acquire(self, key, owner, timeout): return None
    def renew(self, key, owner): return True
    def release(self, key, owner): self.released.append((key, owner))


class FailingRenewLock(FakeLock):
    def renew(self, key, owner): return False


class ThreadAwareFactory(FakeSdkFactory):
    """Bind /mnt/user-data to the thread named in each create's metadata."""

    def __init__(self, base):
        super().__init__()
        self.base = base

    def create(self, **kwargs):
        metadata = kwargs["metadata"]
        self.root_map = {
            "/mnt/user-data": (
                self.base
                / ".myflow/users"
                / metadata["myflow.user"]
                / "threads"
                / metadata["myflow.thread"]
                / "user-data"
            )
        }
        return super().create(**kwargs)


def _provider(
    tmp_path,
    *,
    generic: bool = False,
    factory=None,
    lock=None,
    max_cached_threads: int = 64,
):
    cube = CubeSandboxConfig(
        template_id="tpl",
        max_cached_threads=max_cached_threads,
        host_mount=CubeSandboxHostMountConfig(local_root=str(tmp_path), host_root="/data/shared/myflow"),
        lease=CubeSandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    config = AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(cube=cube),
        skills=SkillsConfig(
            source="git", builtin={"enabled": False},
            git={"cache_dir": str(tmp_path / "cache")},
        ),
    )
    user_data = (
        tmp_path / ".myflow/users/u1/local/user-data"
        if generic
        else tmp_path / ".myflow/users/u1/threads/t1/user-data"
    )
    if factory is None:
        factory = FakeSdkFactory(root_map={"/mnt/user-data": user_data})
    if lock is None:
        lock = FakeLock()
    provider = CubeSandboxProvider(
        app_config=config, sdk_factory=factory, lock=lock,
        paths=Paths(tmp_path / ".myflow"), instance_id="inst-1",
    )
    return provider, factory, lock, config


def test_acquire_creates_and_caches(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t1", user_id="u1")
    assert first == second == "cube:s1"
    assert len(factory.created) == 1


def test_acquire_reuses_discovered_sandbox(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    mount_hash = provider.desired_mounts_hash("u1", "t1")
    factory.existing = [{"sandboxID": "remote-1", "metadata": {
        "myflow": "1", "myflow.user": "u1", "myflow.thread": "t1",
        "myflow.mounts_hash": mount_hash,
    }}]
    assert provider.acquire("t1", user_id="u1") == "cube:remote-1"
    assert factory.connected == ["remote-1"]


def test_mount_hash_mismatch_recreates(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    factory.existing = [{"sandboxID": "remote-1", "metadata": {
        "myflow": "1", "myflow.user": "u1", "myflow.thread": "t1",
        "myflow.mounts_hash": "stale",
    }}]
    provider.acquire("t1", user_id="u1")
    assert factory.created
    assert factory.existing[0]["killed"] is True


def test_canary_failure_kills_new_sandbox(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    factory.fail_canary = True
    with pytest.raises(Exception, match="canary"):
        provider.acquire("t1", user_id="u1")
    assert factory.created[0].killed is True


def test_startup_check_pings_cube_and_redis(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    provider.startup_check()
    assert factory.health_calls == 1
    assert lock.pings == 1


def test_get_is_memory_only(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    factory.calls.clear()
    assert provider.get(sandbox_id) is not None
    assert factory.calls == []


def test_release_pauses_and_releases_lock(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    provider.release(sandbox_id)
    assert factory.created[0].paused is True
    assert lock.released


def test_reset_keeps_remote_but_releases_lock(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    provider.reset()
    assert factory.created[0].paused is False
    assert factory.created[0].killed is False
    assert lock.released


def test_create_metadata_describes_the_host_mount_contract(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")

    created = factory.created[0]
    metadata = created.metadata
    assert created.template == "tpl"
    assert created.timeout == 1800
    assert created.allow_internet_access is True
    assert created.lifecycle == {"on_timeout": "pause", "auto_resume": True}
    assert created.network == {
        "allow_public_traffic": False,
        "allow_out": [],
        "deny_out": [],
    }
    assert metadata["myflow"] == "1"
    assert metadata["myflow.version"] == "1"
    assert metadata["myflow.user"] == "u1"
    assert metadata["myflow.thread"] == "t1"
    assert metadata["myflow.generic"] == "0"
    assert metadata["myflow.instance"] == "inst-1"
    assert metadata["myflow.mounts_hash"] == provider.desired_mounts_hash("u1", "t1")
    assert json.loads(metadata["host-mount"])[0] == {
        "hostPath": "/data/shared/myflow/.myflow/users/u1/threads/t1/user-data",
        "mountPath": "/mnt/user-data",
        "readOnly": False,
    }

    discovery = next(call for call in factory.calls if call["op"] == "list_by_metadata")
    assert discovery["metadata"] == {
        "myflow": "1",
        "myflow.user": "u1",
        "myflow.thread": "t1",
    }


def test_multiple_discovered_sandboxes_choose_the_earliest(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    mount_hash = provider.desired_mounts_hash("u1", "t1")
    metadata = {
        "myflow": "1",
        "myflow.user": "u1",
        "myflow.thread": "t1",
        "myflow.mounts_hash": mount_hash,
    }
    factory.existing = [
        {
            "sandboxID": "remote-late",
            "createdAt": "2026-02-01T00:00:00Z",
            "metadata": metadata,
        },
        {
            "sandboxID": "remote-early",
            "createdAt": "2026-01-01T00:00:00Z",
            "metadata": metadata,
        },
    ]

    assert provider.acquire("t1", user_id="u1") == "cube:remote-early"
    assert factory.connected == ["remote-early"]


def test_generic_thread_uses_the_sentinel_metadata_value(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path, generic=True)
    assert provider.acquire(None, user_id="u1") == "cube:s1"
    metadata = factory.created[0].metadata
    assert metadata["myflow.thread"] == "__generic__"
    assert metadata["myflow.generic"] == "1"


def test_canary_probes_are_removed_after_success(tmp_path) -> None:
    provider, _, _, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    canary_dir = (
        tmp_path
        / ".myflow"
        / "users"
        / "u1"
        / "threads"
        / "t1"
        / "user-data"
        / "workspace"
        / ".myflow-canary"
    )
    assert canary_dir.exists() is False


def test_canary_failure_releases_the_lease_and_leaves_no_cache(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    factory.fail_canary = True
    with pytest.raises(Exception, match="canary"):
        provider.acquire("t1", user_id="u1")
    assert lock.released
    assert provider.get("cube:s1") is None


def test_release_can_kill_and_closes_the_sdk(tmp_path) -> None:
    provider, factory, lock, config = _provider(tmp_path)
    config.sandbox.cube.release_action = "kill"
    sandbox_id = provider.acquire("t1", user_id="u1")
    provider.release(sandbox_id)
    assert factory.created[0].killed is True
    assert factory.created[0].closed is True
    assert lock.released


def test_release_async_offloads_the_remote_action(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    asyncio.run(provider.release_async(sandbox_id))
    assert factory.created[0].paused is True
    assert lock.released


def test_shutdown_kills_cached_sandboxes_and_clears_memory(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    provider.shutdown()
    assert factory.created[0].killed is True
    assert factory.created[0].closed is True
    assert lock.released
    assert provider.get(sandbox_id) is None


def test_capability_flags_follow_the_skill_source(tmp_path) -> None:
    provider, _, _, config = _provider(tmp_path)
    assert provider.supports_agent_skill_isolation is True
    assert provider.supports_per_user_skill_mounts is True
    assert provider.uses_thread_data_mounts is True

    config.skills.source = "filesystem"
    assert provider.supports_per_user_skill_mounts is False


def test_foreign_ids_are_memory_misses_without_factory_calls(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    factory.calls.clear()
    assert provider.get("local:u1:t1") is None
    assert provider.get("cube:") is None
    provider.release("local:u1:t1")
    assert factory.calls == []


class _StatusError(Exception):
    """Stand-in for cubesandbox.ApiError carrying an HTTP status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _fail_command(status_code: int):
    def handler(cmd, **kwargs):
        del cmd, kwargs
        raise _StatusError(status_code)

    return handler


@pytest.mark.parametrize("status", [404, 410])
def test_reaped_sandbox_drops_the_handle_and_releases_the_lease(tmp_path, status) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sdk = factory.created[0]
    sdk.commands.handler = _fail_command(status)

    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    # The runtime re-acquires because the dead handle is no longer served.
    assert provider.get(sandbox_id) is None
    assert any(key == "myflow:sandbox:u1:t1" for key, _ in lock.released)
    # A MicroVM that 404s is already gone: neither pause nor kill, only close.
    assert sdk.paused is False
    assert sdk.killed is False
    assert sdk.closed is True


def test_reacquisition_after_a_reap_creates_a_fresh_sandbox(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    factory.created[0].commands.handler = _fail_command(404)
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    assert provider.acquire("t1", user_id="u1") == "cube:s2"
    assert provider.get("cube:s2") is not None
    assert provider.get(sandbox_id) is None


def test_ordinary_command_failures_keep_the_cached_handle(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sdk = factory.created[0]
    sdk.commands.handler = _fail_command(500)

    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    with pytest.raises(SandboxRuntimeError):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is sandbox
    assert lock.released == []
    assert sdk.closed is False


def test_lease_loss_drops_the_handle_without_touching_the_remote(tmp_path) -> None:
    provider, factory, _, config = _provider(tmp_path, lock=FailingRenewLock())
    config.sandbox.cube.lease.ttl_seconds = 0
    sandbox_id = provider.acquire("t1", user_id="u1")

    deadline = time.monotonic() + 1
    while provider.get(sandbox_id) is not None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert provider.get(sandbox_id) is None
    assert factory.created[0].paused is False
    assert factory.created[0].killed is False


def test_lru_eviction_releases_the_lease_without_killing_the_remote(tmp_path) -> None:
    factory = ThreadAwareFactory(tmp_path)
    provider, _, lock, _ = _provider(
        tmp_path, factory=factory, max_cached_threads=1
    )

    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t2", user_id="u1")

    assert (first, second) == ("cube:s1", "cube:s2")
    assert provider.get(first) is None
    assert provider.get(second) is not None
    assert factory.created[0].paused is False
    assert factory.created[0].killed is False
    assert factory.created[0].closed is True
    assert any(key == "myflow:sandbox:u1:t1" for key, _ in lock.released)

    provider.reset()

# E2B reports a dead sandbox in the exception type, without an HTTP status: the
# control plane answers SandboxNotFoundException, the proxy in front of envd
# answers SandboxNotRunningException. Both must still drop the handle.


class SandboxNotFoundException(Exception):
    """Stand-in for e2b.SandboxNotFoundException."""


class SandboxNotRunningException(Exception):
    """Stand-in for e2b.SandboxNotRunningException."""


def test_e2b_gone_command_drops_the_handle_and_releases_the_lease(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sdk = factory.created[0]

    def raise_gone(cmd, **kwargs):
        del cmd, kwargs
        raise SandboxNotRunningException("was not found: the sandbox was killed")

    sdk.commands.handler = raise_gone

    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    assert provider.get(sandbox_id) is None
    assert any(key == "myflow:sandbox:u1:t1" for key, _ in lock.released)
    assert sdk.closed is True


def test_release_survives_a_pause_that_reports_the_sandbox_is_gone(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sdk = factory.created[0]

    def raise_gone() -> None:
        raise SandboxNotFoundException("sandbox not found")

    sdk.pause = raise_gone

    provider.release(sandbox_id)

    assert lock.released
    assert sdk.closed is True
