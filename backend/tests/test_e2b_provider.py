import asyncio
import time

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    E2BSandboxConfig,
    E2BSandboxSyncConfig,
    SandboxLeaseConfig,
)
from agentkit.sandbox.e2b.provider import E2BSandboxProvider
from agentkit.sandbox.exceptions import SandboxNotFoundError, SandboxRuntimeError
from fakes.e2b_sdk import FakeSdkFactory


class FakeLock:
    def __init__(self): self.released = []; self.pings = 0
    def ping(self): self.pings += 1
    def acquire(self, key, owner, timeout): return None
    def renew(self, key, owner): return True
    def release(self, key, owner): self.released.append((key, owner))


class FailingRenewLock(FakeLock):
    def renew(self, key, owner): return False


def _provider(
    tmp_path,
    *,
    factory=None,
    lock=None,
    max_cached_threads: int = 64,
    sync: E2BSandboxSyncConfig | None = None,
):
    e2b = E2BSandboxConfig(
        template_id="tpl",
        max_cached_threads=max_cached_threads,
        lease=SandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
        sync=sync or E2BSandboxSyncConfig(),
    )
    config = AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(e2b=e2b),
        skills=SkillsConfig(
            source="git", builtin={"enabled": False},
            git={"cache_dir": str(tmp_path / "cache")},
        ),
    )
    if factory is None:
        factory = FakeSdkFactory()
    if lock is None:
        lock = FakeLock()
    provider = E2BSandboxProvider(
        app_config=config, sdk_factory=factory, lock=lock,
        paths=Paths(tmp_path / ".myflow"), instance_id="inst-1",
    )
    return provider, factory, lock, config


def _user_data(tmp_path, thread: str = "t1", user: str = "u1"):
    return tmp_path / ".myflow" / "users" / user / "threads" / thread / "user-data"


def test_acquire_creates_and_caches(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t1", user_id="u1")
    assert first == second == "e2b:s1"
    assert len(factory.created) == 1


def test_acquire_reuses_discovered_sandbox(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    factory.existing = [{"sandboxID": "remote-1", "metadata": {
        "myflow.version": "1",
        "myflow.user": "u1",
        "myflow.thread": "t1",
    }}]
    assert provider.acquire("t1", user_id="u1") == "e2b:remote-1"
    assert factory.connected == ["remote-1"]


def test_canary_failure_kills_new_sandbox(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    factory.fail_canary = True
    with pytest.raises(Exception, match="canary"):
        provider.acquire("t1", user_id="u1")
    assert factory.created[0].killed is True


def test_startup_check_pings_e2b_and_redis(tmp_path) -> None:
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


def test_create_metadata_has_no_host_mount_or_mount_hash(tmp_path) -> None:
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
    assert metadata == {
        "myflow.version": "1",
        "myflow.user": "u1",
        "myflow.thread": "t1",
        "myflow.instance": "inst-1",
    }
    assert "host-mount" not in metadata
    assert "myflow.mounts_hash" not in metadata

    discovery = next(call for call in factory.calls if call["op"] == "list_by_metadata")
    assert discovery["metadata"] == {
        "myflow.user": "u1",
        "myflow.thread": "t1",
    }


def test_multiple_discovered_sandboxes_choose_the_earliest(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    metadata = {
        "myflow.version": "1",
        "myflow.user": "u1",
        "myflow.thread": "t1",
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

    assert provider.acquire("t1", user_id="u1") == "e2b:remote-early"
    assert factory.connected == ["remote-early"]


def test_generic_thread_uses_the_sentinel_metadata_value(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    assert provider.acquire(None, user_id="u1") == "e2b:s1"
    metadata = factory.created[0].metadata
    assert metadata["myflow.thread"] == "__generic__"
    assert "myflow.generic" not in metadata


def test_canary_probe_is_removed_after_success(tmp_path) -> None:
    provider, factory, _, _ = _provider(tmp_path)
    provider.acquire("t1", user_id="u1")
    canary = "/mnt/user-data/workspace/.myflow-canary/canary"
    assert factory.created[0].files.exists(canary) is False


def test_canary_failure_releases_the_lease_and_leaves_no_cache(tmp_path) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    factory.fail_canary = True
    with pytest.raises(Exception, match="canary"):
        provider.acquire("t1", user_id="u1")
    assert lock.released
    assert provider.get("e2b:s1") is None


def test_release_can_kill_and_closes_the_sdk(tmp_path) -> None:
    provider, factory, lock, config = _provider(tmp_path)
    config.sandbox.e2b.release_action = "kill"
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
    assert provider.get("e2b:") is None
    provider.release("local:u1:t1")
    assert factory.calls == []


class _StatusError(Exception):
    """Stand-in for an E2B API error carrying an HTTP status."""

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
    # A sandbox that 404s is already gone: neither pause nor kill, only close.
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

    assert provider.acquire("t1", user_id="u1") == "e2b:s2"
    assert provider.get("e2b:s2") is not None
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
    config.sandbox.e2b.lease.ttl_seconds = 0
    sandbox_id = provider.acquire("t1", user_id="u1")

    deadline = time.monotonic() + 1
    while provider.get(sandbox_id) is not None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert provider.get(sandbox_id) is None
    assert factory.created[0].paused is False
    assert factory.created[0].killed is False


def test_lru_eviction_releases_the_lease_without_killing_the_remote(tmp_path) -> None:
    provider, factory, lock, _ = _provider(
        tmp_path, max_cached_threads=1
    )

    first = provider.acquire("t1", user_id="u1")
    second = provider.acquire("t2", user_id="u1")

    assert (first, second) == ("e2b:s1", "e2b:s2")
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


# -- explicit sync at the lifecycle boundary ------------------------------


def test_acquire_uploads_the_host_uploads_tree(tmp_path) -> None:
    uploads = _user_data(tmp_path) / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "brief.txt").write_text("uploaded\n", encoding="utf-8")

    provider, factory, _, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    assert sandbox.read_file("/mnt/user-data/uploads/brief.txt").strip() == "uploaded"


def test_acquire_does_not_upload_workspace_by_default(tmp_path) -> None:
    workspace = _user_data(tmp_path) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "old.txt").write_text("old\n", encoding="utf-8")

    provider, _, _, _ = _provider(tmp_path)
    sandbox = provider.get(provider.acquire("t1", user_id="u1"))
    assert sandbox is not None
    with pytest.raises(OSError):
        sandbox.read_file("/mnt/user-data/workspace/old.txt")


def test_upload_workspace_when_enabled(tmp_path) -> None:
    workspace = _user_data(tmp_path) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "old.txt").write_text("old\n", encoding="utf-8")

    sync = E2BSandboxSyncConfig(upload_workspace=True)
    provider, _, _, _ = _provider(tmp_path, sync=sync)
    sandbox = provider.get(provider.acquire("t1", user_id="u1"))
    assert sandbox is not None
    assert sandbox.read_file("/mnt/user-data/workspace/old.txt").strip() == "old"


def test_acquire_upload_failure_kills_the_new_sandbox(tmp_path) -> None:
    uploads = _user_data(tmp_path) / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "brief.txt").write_text("uploaded\n", encoding="utf-8")

    class FailingUploadFactory(FakeSdkFactory):
        def create(self, **kwargs):
            sandbox = super().create(**kwargs)
            original = sandbox.files.write

            def write(path, data, **options):
                if "/uploads/" in path:
                    raise OSError("upload transport failed")
                return original(path, data, **options)

            sandbox.files.write = write
            return sandbox

    provider, factory, lock, _ = _provider(tmp_path, factory=FailingUploadFactory())
    with pytest.raises(SandboxRuntimeError, match="acquire failed"):
        provider.acquire("t1", user_id="u1")

    assert factory.created[0].killed is True
    assert provider.get("e2b:s1") is None
    assert lock.released


def test_release_downloads_outputs_and_preserves_host_only_files(tmp_path) -> None:
    outputs = _user_data(tmp_path) / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "keep.txt").write_text("host only\n", encoding="utf-8")

    provider, factory, _, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    sandbox.write_file("/mnt/user-data/outputs/result.txt", "from sandbox\n")
    sandbox.write_file("/mnt/user-data/outputs/nested/deep.txt", "deep\n")

    provider.release(sandbox_id)

    assert (outputs / "result.txt").read_text(encoding="utf-8") == "from sandbox\n"
    assert (outputs / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep\n"
    assert (outputs / "keep.txt").read_text(encoding="utf-8") == "host only\n"
    assert factory.created[0].paused is True


def test_release_download_failure_is_only_a_warning(tmp_path, caplog) -> None:
    provider, factory, lock, _ = _provider(tmp_path)
    sandbox_id = provider.acquire("t1", user_id="u1")
    sdk = factory.created[0]

    def fail_list(path, **kwargs):
        del path, kwargs
        raise OSError("listing transport failed")

    sdk.files.list = fail_list

    provider.release(sandbox_id)

    assert sdk.paused is True
    assert lock.released
    assert sdk.closed is True
    assert any("download" in record.message.lower() for record in caplog.records)
