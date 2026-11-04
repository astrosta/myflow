# CubeSandbox Host-Mount Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a CubeSandboxProvider that runs agent shell/file tools in a CubeSandbox MicroVM while the Gateway's existing per-thread user-data directory is bind-mounted into the sandbox, with no second workspace store and no changes to thread_files.py.

**Architecture:** Gateway and Cubelet run on the same Linux host; local_root and host_root point at the same project root. Each sandbox is created with a metadata host-mount descriptor for the thread's .myflow/users/.../user-data plus read-only skill mounts. A Redis per-thread lock serializes sandbox creation/connection across Gateway workers; the provider discovers existing sandboxes by metadata, verifies a mount hash, and runs a bidirectional canary before handing the sandbox to tools.

**Tech Stack:** Python 3.12, pydantic v2, cubesandbox>=0.7.3,<0.8, redis>=5, httpx, pytest, fakeredis.

**Spec:** docs/superpowers/specs/2026-11-04-cubesandbox-host-mount-design.md

## Global Constraints

- Deployment is one Linux host: Gateway and Cubelet see the same project root; local_root == host_root == project_root() by default.
- Host filesystem is Ubuntu local storage (treated as ext4); no NFS root_squash.
- sandbox.cube.lease.redis_url is required for the Cube provider; startup fails if Redis is unreachable.
- Redis lock key is myflow:sandbox:{user_id}:{thread_id or "__generic__"}, TTL 30s, renew every 10s, compare-and-delete with Lua.
- release_action is pause; on_timeout is pause; auto_resume is true; sandbox_user is root.
- allow_public_traffic is false; allow_internet_access is true by default, with configured deny rules.
- Command timeout comes from sandbox.bash_command_timeout (600s default), not request_timeout_seconds.
- Sandbox command output is capped at 1 MiB, matching the local provider.
- thread_files.py is not modified; uploads and artifacts continue to read/write the host user-data directory.
- No database migration and no workspace data migration.
- sandbox.use, sandbox.cube.*, sandbox.mounts, skills.container_path, skills.builtin.*, and the skills source/cache are startup-only.
- The worktree is already dirty. In every commit step, stage only the files listed in that task; never use git add -A.
- Run backend tests from backend/: cd backend && uv run pytest tests/<file>.py -v.

## Review Focus

1. **Host mount permissions:** a sandbox running as root writes a file that the Gateway process cannot read/delete (umask or UID mismatch). Task 8's canary tests pin the bidirectional read/write contract; a failure must kill the new sandbox and surface an actionable error.
2. **Redis lock loss during a long command:** renewal stops, TTL expires, another worker acquires the lock, and the first worker keeps writing. Task 4's LockLease tests pin loss detection; Task 8's on_lost wiring must mark the handle dead, never kill the remote, and make the next acquire re-lock.
3. **Stale host mounts on discovery:** a Gateway restart with changed skill paths connects to an old sandbox whose host-mount still points at the old tree. Task 8's mounts_hash test pins "recreate under lock; do not reuse stale mounts".
4. **Path escape / read-only skill writes:** model-supplied paths escape /mnt/user-data or write into /mnt/skills. Tasks 2, 6, and 7 pin traversal rejection and read-only enforcement for every file/search method.
5. **Unbounded command output / transport timeout:** a command emits gigabytes or runs for 600s while the SDK client has a 30s timeout. Task 6 pins the 1 MiB output wrapper and per-call 600s timeout.

---

## File Structure

~~~text
backend/packages/harness/agentkit/
  config/
    sandbox_config.py           # add Cube config models + SandboxConfig.cube
    reload_boundary.py          # document startup-only Cube fields
    __init__.py                 # export new config models
  sandbox/
    security.py                 # VirtualMount + assert_virtual_path
    sandbox_provider.py         # startup_check + release_async
    lease.py                    # async release handle
    runtime.py                  # start_sandbox_provider
    cube/
      __init__.py               # provider/sandbox exports
      client.py                 # SDK protocol + CubesandboxFactory
      files.py                  # file API, tree, glob/grep helper
      locks.py                  # RedisThreadLock + LockLease
      mounts.py                 # host-mount descriptors + hashes
      provider.py               # CubeSandboxProvider lifecycle
      sandbox.py                # CubeSandbox
  agents/middlewares/sandbox_middleware.py  # await async release
  app/gateway/app.py            # startup_check + shutdown

backend/tests/
  fakes/cube_sdk.py             # fake SDK sandbox/factory
  test_cube_config.py
  test_cube_virtual_paths.py
  test_cube_mounts.py
  test_cube_locks.py
  test_cube_client.py
  test_cube_sandbox.py
  test_cube_search.py
  test_cube_provider.py
  test_cube_runtime.py
  test_cube_integration.py      # opt-in live test

config.example.yaml             # commented Cube block
backend/README.md               # deployment/config notes
~~~

---

### Task 1: Configuration Surface

**Files:**
- Modify: backend/packages/harness/agentkit/config/sandbox_config.py
- Modify: backend/packages/harness/agentkit/config/reload_boundary.py
- Modify: backend/packages/harness/agentkit/config/__init__.py
- Modify: backend/packages/harness/pyproject.toml
- Modify: backend/pyproject.toml
- Test: backend/tests/test_cube_config.py

**Interfaces:**
- Produces: CubeSandboxNetworkConfig, CubeSandboxHostMountConfig, CubeSandboxLeaseConfig, CubeSandboxConfig, and SandboxConfig.cube: CubeSandboxConfig | None.
- Produces: CubeSandboxHostMountConfig.host_root: str | None = None; None means "same as local_root".
- Produces: CubeSandboxLeaseConfig.redis_url: str (required); ttl_seconds: int = 30.
- Produces: STARTUP_ONLY_FIELDS["sandbox.cube"].

- [ ] **Step 1: Write the failing config tests**

~~~python
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
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_config.py -v. Expected: FAIL with a missing cube attribute or missing STARTUP_ONLY_FIELDS key.

- [ ] **Step 3: Implement the config models and optional extras**

Add to sandbox_config.py:

~~~python
class CubeSandboxNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_out: list[str] = Field(default_factory=list)
    deny_out: list[str] = Field(default_factory=list)
    rules: list[dict] | dict | None = None


class CubeSandboxHostMountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_root: str | None = None
    host_root: str | None = None
    allowed_prefix: str | None = None


class CubeSandboxLeaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    redis_url: str
    ttl_seconds: int = 30


class CubeSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_url: str = "http://127.0.0.1:3000"
    api_key: str = Field(default="", description="Log and error output must redact this value")
    template_id: str
    sandbox_domain: str = "cube.app"
    proxy_node_ip: str | None = None
    proxy_port: int = 80
    timeout_seconds: int = 1800
    on_timeout: Literal["kill", "pause"] = "pause"
    auto_resume: bool = True
    release_action: Literal["pause", "kill"] = "pause"
    acquire_timeout_seconds: float = 120
    request_timeout_seconds: float = 30
    startup_healthcheck: bool = True
    allow_internet_access: bool = True
    allow_public_traffic: bool = False
    sandbox_user: str = "root"
    network: CubeSandboxNetworkConfig = Field(default_factory=CubeSandboxNetworkConfig)
    host_mount: CubeSandboxHostMountConfig
    lease: CubeSandboxLeaseConfig
    max_cached_threads: int = 64
~~~

Add cube: CubeSandboxConfig | None = None to SandboxConfig, and add "sandbox.cube" to STARTUP_ONLY_FIELDS. Export the four classes from agentkit/config/__init__.py.

Add cube = ["cubesandbox>=0.7.3,<0.8"] to backend/packages/harness/pyproject.toml optional dependencies, and pass it through in backend/pyproject.toml as cube = ["agentkit[cube,redis]"]. Keep the existing redis extra.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_config.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/config/sandbox_config.py   backend/packages/harness/agentkit/config/reload_boundary.py   backend/packages/harness/agentkit/config/__init__.py   backend/packages/harness/pyproject.toml backend/pyproject.toml   backend/tests/test_cube_config.py
git commit -m "feat(sandbox): add CubeSandbox configuration models"
~~~

---

### Task 2: Virtual Mount Path Guard

**Files:**
- Modify: backend/packages/harness/agentkit/sandbox/security.py
- Test: backend/tests/test_cube_virtual_paths.py

**Interfaces:**
- Produces: @dataclass(frozen=True) class VirtualMount: container_path: str; read_only: bool = False.
- Produces: assert_virtual_path(path: str, mounts: Sequence[VirtualMount], *, write: bool = False) -> str.
- The returned value is posixpath.normpath(path); no trailing slash except /.

- [ ] **Step 1: Write the failing path tests**

~~~python
import pytest

from agentkit.sandbox.security import VirtualMount, assert_virtual_path

MOUNTS = [
    VirtualMount("/mnt/user-data", read_only=False),
    VirtualMount("/mnt/user-data/workspace", read_only=False),
    VirtualMount("/mnt/skills", read_only=True),
]


def test_accepts_child_of_mount() -> None:
    assert assert_virtual_path("/mnt/user-data/workspace/a.py", MOUNTS, write=True) == "/mnt/user-data/workspace/a.py"


def test_longest_prefix_wins_for_read_only() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/user-data/workspace/a.py", [
            VirtualMount("/mnt/user-data"),
            VirtualMount("/mnt/user-data/workspace", read_only=True),
        ], write=True)


def test_rejects_parent_traversal() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/user-data/../skills/x", MOUNTS)


def test_rejects_shapes_that_are_not_absolute_posix() -> None:
    for path in ["mnt/user-data/a", "C:\\x", "\\\\server\\share", "file:///x", "/mnt/user-data/a\x00b"]:
        with pytest.raises(PermissionError):
            assert_virtual_path(path, MOUNTS)


def test_rejects_write_to_read_only_mount() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/skills/builtin/x", MOUNTS, write=True)
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_virtual_paths.py -v. Expected: FAIL with ImportError for VirtualMount.

- [ ] **Step 3: Implement the guard**

Use a segment-bounded longest-prefix match, like find_path_mapping. Check NUL, ://, Windows drive, UNC, non-absolute, and any .. segment before normalization. A match with read_only=True rejects write=True. Do not use Path.resolve(); the path is remote.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_virtual_paths.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/security.py backend/tests/test_cube_virtual_paths.py
git commit -m "feat(sandbox): add virtual mount path guard"
~~~

---

### Task 3: Host Mount Descriptor Builder

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/cube/__init__.py
- Create: backend/packages/harness/agentkit/sandbox/cube/mounts.py
- Test: backend/tests/test_cube_mounts.py

**Interfaces:**
- Produces: HostRoots(local_root: Path, host_root: PurePosixPath).
- Produces: resolve_host_roots(cube: CubeSandboxConfig) -> HostRoots.
- Produces: to_host_path(local: Path, roots: HostRoots) -> str.
- Produces: MountEntry(container_path: str, host_path: str, read_only: bool) with as_payload() -> dict[str, object].
- Produces: build_host_mounts(*, app_config: AppConfig, user_id: str, thread_id: str | None, paths: Paths | None = None) -> tuple[MountEntry, ...].
- Produces: mounts_hash(mounts: Sequence[MountEntry]) -> str (SHA-256 hex, first 16 chars).
- Produces: validate_mount_sources(app_config: AppConfig, roots: HostRoots) -> None.
- build_host_mounts creates thread dirs, creates an empty private-skill dir when skills.source == "git", skips a missing builtin tree with a warning, and never mounts the skills cache root as an operator mount.

- [ ] **Step 1: Write the failing mount tests**

~~~python
from pathlib import Path

import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.sandbox_config import (
    CubeSandboxConfig,
    CubeSandboxHostMountConfig,
    CubeSandboxLeaseConfig,
)
from agentkit.config.paths import Paths
from agentkit.sandbox.cube.mounts import build_host_mounts, mounts_hash


def _config(tmp_path: Path, *, source: str = "git") -> AppConfig:
    skills = SkillsConfig(
        source=source,
        path=str(tmp_path / "skills"),
        git={"cache_dir": str(tmp_path / "cache")},
        builtin={"path": str(tmp_path / "skills" / "builtin")},
    )
    (tmp_path / "skills" / "builtin").mkdir(parents=True)
    cube = CubeSandboxConfig(
        template_id="tpl",
        host_mount=CubeSandboxHostMountConfig(
            local_root=str(tmp_path), host_root="/data/shared/myflow"
        ),
        lease=CubeSandboxLeaseConfig(redis_url="redis://127.0.0.1:6379/0"),
    )
    return AppConfig(
        models=[{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
        sandbox=SandboxConfig(cube=cube), skills=skills,
    )


def test_same_host_user_data_and_builtin_mounts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    by_container = {m.container_path: m for m in mounts}
    assert by_container["/mnt/user-data"].host_path ==         "/data/shared/myflow/.myflow/users/u1/threads/t1/user-data"
    assert by_container["/mnt/user-data"].read_only is False
    assert by_container["/mnt/skills/builtin"].read_only is True


def test_git_private_tree_is_created_and_read_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    private = next(m for m in mounts if m.container_path == "/mnt/skills/private")
    assert private.read_only is True
    assert private.host_path == "/data/shared/myflow/cache/active/users/u1"
    assert (tmp_path / "cache" / "active" / "users" / "u1").is_dir()


def test_filesystem_builtin_inside_tree_is_not_double_mounted(tmp_path: Path) -> None:
    config = _config(tmp_path, source="filesystem")
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    assert [m.container_path for m in mounts].count("/mnt/skills/builtin") == 0


def test_operator_mount_cannot_shadow_user_data_or_skills(tmp_path: Path) -> None:
    config = _config(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    config.sandbox.mounts = [
        {"container_path": "/mnt/user-data", "local_path": str(safe)},
        {"container_path": "/mnt/skills/builtin", "local_path": str(safe)},
        {"container_path": "/safe", "local_path": str(safe), "read_only": True},
    ]
    mounts = build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    )
    safe_mount = next(m for m in mounts if m.container_path == "/safe")
    assert safe_mount.read_only is True


def test_mount_source_outside_local_root_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    config.sandbox.mounts = [{"container_path": "/safe", "local_path": str(outside)}]
    with pytest.raises(Exception, match="outside"):
        build_host_mounts(
            app_config=config, user_id="u1", thread_id="t1",
            paths=Paths(tmp_path / ".myflow"),
        )


def test_mounts_hash_is_order_independent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mounts = list(build_host_mounts(
        app_config=config, user_id="u1", thread_id="t1",
        paths=Paths(tmp_path / ".myflow"),
    ))
    assert mounts_hash(mounts) == mounts_hash(list(reversed(mounts)))
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_mounts.py -v. Expected: FAIL with ModuleNotFoundError: agentkit.sandbox.cube.

- [ ] **Step 3: Implement the builder**

Use get_paths().sandbox_user_data_dir(thread_id, user_id=user_id) for threads, and .myflow/users/{uid}/local/user-data for thread_id is None. Resolve local_root from cube.host_mount.local_root or project_root(); resolve host_root from cube.host_mount.host_root or str(local_root). Reject /, relative paths, and .. in host_root.

Build mounts in this order, then sort by container_path depth ascending:

1. /mnt/user-data -> user-data directory, writable.
2. Git: <container>/private -> resolve_path(skills.git.cache_dir)/active/users/{uid}, read-only; create it if missing.
3. Filesystem: <container> -> skills.root_dir(), read-only, only if it exists.
4. Builtin: <container>/builtin -> resolve_builtin_root(skills), read-only, only if enabled/exists; skip if already covered by the filesystem root.
5. sandbox.mounts: validate container_path, translate local_path, preserve read_only; skip anything at/under /mnt/user-data or <container>, and anything whose local path is the skills cache/root/builtin.

validate_mount_sources checks local_root exists and is absolute, host_root is valid, and all static skill/operator sources are under local_root; it must not create per-user dirs.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_mounts.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/__init__.py   backend/packages/harness/agentkit/sandbox/cube/mounts.py backend/tests/test_cube_mounts.py
git commit -m "feat(sandbox): build CubeSandbox host mount descriptors"
~~~

---

### Task 4: Redis Per-Thread Lock

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/cube/locks.py
- Test: backend/tests/test_cube_locks.py

**Interfaces:**
- Produces: class LockNotAcquiredError(SandboxRuntimeError).
- Produces: class ThreadLock(Protocol) with ping(), acquire(key, owner, timeout), renew(key, owner) -> bool, release(key, owner).
- Produces: class RedisThreadLock(ThreadLock) with __init__(redis_url: str, ttl_seconds: int, *, client: redis.Redis | None = None).
- Produces: class LockLease with __init__(lock, key, owner, ttl_seconds, on_lost: Callable[[], None] | None = None), acquire(timeout: float) -> None, start() -> None, release() -> None, lost: bool.
- Lock key: myflow:sandbox:{user_id}:{thread_id or "__generic__"}; value: {instance_id}:{uuid4}.

- [ ] **Step 1: Write the failing lock tests**

~~~python
import time

import fakeredis
import pytest

from agentkit.sandbox.cube.locks import LockLease, LockNotAcquiredError, RedisThreadLock


def _lock(client=None) -> RedisThreadLock:
    return RedisThreadLock(
        "redis://unused", 5, client=client or fakeredis.FakeRedis(decode_responses=True)
    )


def test_lock_is_exclusive_and_releases() -> None:
    lock = _lock()
    lock.acquire("k", "owner-a", timeout=0.1)
    with pytest.raises(LockNotAcquiredError):
        lock.acquire("k", "owner-b", timeout=0.05)
    lock.release("k", "owner-a")
    lock.acquire("k", "owner-b", timeout=0.1)
    lock.release("k", "owner-b")


def test_release_only_deletes_own_value() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    lock = _lock(client)
    lock.acquire("k", "owner-a", timeout=0.1)
    client.set("k", "other")
    lock.release("k", "owner-a")
    assert client.get("k") == "other"


def test_renew_requires_ownership() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    lock = _lock(client)
    lock.acquire("k", "owner-a", timeout=0.1)
    assert lock.renew("k", "owner-a") is True
    client.set("k", "other")
    assert lock.renew("k", "owner-a") is False


def test_lease_marks_lost_when_renew_fails() -> None:
    class FakeLock:
        def acquire(self, key, owner, timeout): return None
        def renew(self, key, owner): return False
        def release(self, key, owner): return None

    lost = []
    lease = LockLease(FakeLock(), "k", "owner", 0.01, on_lost=lambda: lost.append(True))
    lease.acquire(timeout=0.1)
    lease.start()
    deadline = time.monotonic() + 1
    while not lost and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lease.lost is True
    assert lost == [True]
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_locks.py -v. Expected: FAIL with missing module/classes.

- [ ] **Step 3: Implement the Redis lock and lease**

Use a sync redis.Redis.from_url(redis_url, decode_responses=True) when client is omitted; import redis inside the constructor so local mode does not require the extra. Use Lua scripts for renew and release. acquire uses SET key owner NX PX ttl_ms, loops with time.sleep(0.1) until the deadline, and raises LockNotAcquiredError. LockLease.start launches a daemon thread that renews every ttl_seconds / 3 and calls on_lost once when renew returns false. release stops the thread and compares-and-deletes only when the value still matches.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_locks.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/locks.py backend/tests/test_cube_locks.py
git commit -m "feat(sandbox): add Redis per-thread lock"
~~~

---

### Task 5: Cube SDK Adapter

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/cube/client.py
- Create: backend/tests/fakes/cube_sdk.py
- Test: backend/tests/test_cube_client.py

**Interfaces:**
- Produces: SdkSandbox Protocol with sandbox_id: str, commands, files, kill(), pause(), close().
- Produces: SdkFactory Protocol with create(*, template: str, timeout: int, metadata: dict[str, str], lifecycle: dict, network: dict, allow_internet_access: bool) -> SdkSandbox, connect(sandbox_id: str) -> SdkSandbox, list_by_metadata(metadata: Mapping[str, str]) -> list[dict], health() -> None.
- Produces: CubesandboxFactory(api_url, api_key, template_id, sandbox_domain, proxy_node_ip, proxy_port, request_timeout, *, http_client: httpx.Client | None = None).
- Produces: build_metadata_query(metadata: Mapping[str, str]) -> str and error_status(exc) -> int | None.
- list_by_metadata calls GET /v2/sandboxes with metadata and state=running,paused, follows x-next-token, and returns the JSON list.

- [ ] **Step 1: Write the failing client tests**

~~~python
import httpx

from agentkit.sandbox.cube.client import (
    CubesandboxFactory,
    build_metadata_query,
    error_status,
)


def test_build_metadata_query_encodes_pairs() -> None:
    assert build_metadata_query({"myflow": "1", "myflow.thread": "t 1"}) ==         "myflow=1&myflow.thread=t+1"


def test_list_by_metadata_follows_next_token() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if "nextToken" not in request.url.params:
            return httpx.Response(200, json=[{"sandboxID": "s1"}], headers={"x-next-token": "n1"})
        return httpx.Response(200, json=[{"sandboxID": "s2"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    factory = CubesandboxFactory(
        api_url="http://cube:3000", api_key="", template_id="tpl",
        sandbox_domain="cube.app", proxy_node_ip=None, proxy_port=80,
        request_timeout=5, http_client=client,
    )
    assert [item["sandboxID"] for item in factory.list_by_metadata({"myflow": "1"})] == ["s1", "s2"]
    assert len(calls) == 2


def test_error_status_reads_status_code() -> None:
    class Boom(Exception):
        status_code = 404

    assert error_status(Boom()) == 404
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_client.py -v. Expected: FAIL with missing module.

- [ ] **Step 3: Implement the adapter and fake SDK**

CubesandboxFactory must lazily import cubesandbox inside methods; do not import it at module top. Build cubesandbox.Config(...) for create, connect, and health. For list_by_metadata, use httpx.Client with base_url=api_url, X-API-Key when configured, params with metadata and state, and follow x-next-token.

In tests/fakes/cube_sdk.py, provide FakeCommandResult, FakeFiles, FakeCommands, FakeSdkSandbox, FakeSdkFactory. FakeSdkFactory(root_map: dict[str, Path] | None = None) passes the map to every sandbox it creates/connects. FakeFiles maps /mnt/user-data to a host directory when root_map is supplied, so provider canary tests can use the real host filesystem. Its list entries use {"name": ..., "type": "FILE_TYPE_FILE" or "FILE_TYPE_DIRECTORY", "size": "0"}. FakeFiles.stat raises a fake FilesystemNotFoundError with status_code = 404 for missing paths. FakeCommands.run records calls and delegates to handler when supplied; otherwise it returns an empty successful result. FakeFiles records written paths in a writes list. FakeSdkFactory records created/connected sandboxes, supports existing metadata rows (and sets existing_row['killed'] when the provider kills one), health, and a fail_canary switch.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_client.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/client.py   backend/tests/fakes/cube_sdk.py backend/tests/test_cube_client.py
git commit -m "feat(sandbox): add CubeSandbox SDK adapter"
~~~

---

### Task 6: CubeSandbox Commands and File I/O

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/cube/files.py
- Create: backend/packages/harness/agentkit/sandbox/cube/sandbox.py
- Modify: backend/packages/harness/agentkit/sandbox/cube/__init__.py
- Test: backend/tests/test_cube_sandbox.py

**Interfaces:**
- Produces: CubeSandbox(sdk: SdkSandbox, *, sandbox_id: str, mounts: Sequence[MountEntry], sandbox_user: str = "root", command_timeout: float = 600, max_output_bytes: int = 1_048_576).
- Produces: CubeSandbox.path_mappings: list[PathMapping] and persistent_shell_sessions = False.
- Produces in files.py: read_text(sdk, path, *, user, start_line=None, end_line=None) -> str, write_text(sdk, path, content, *, user, append=False) -> None, read_bytes(sdk, path, *, user) -> bytes, write_bytes(sdk, path, content, *, user) -> None.
- Produces in files.py: ensure_parent_dir(sdk, path, *, user) -> None.
- CubeSandbox._execute_command wraps the command with set -o pipefail, head -c {max_output_bytes}, PIPESTATUS, and maps exit 141 to 0.

- [ ] **Step 1: Write the failing command/file tests**

~~~python
import pytest

from agentkit.sandbox.cube.mounts import MountEntry
from agentkit.sandbox.cube.sandbox import CubeSandbox
from fakes.cube_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox


def _sandbox(tmp_path):
    sdk = FakeSdkSandbox(
        files=FakeFiles({"/mnt/user-data": tmp_path / "user-data"}),
        commands=FakeCommands(),
    )
    mounts = [MountEntry("/mnt/user-data", str(tmp_path / "user-data"), False)]
    return CubeSandbox(sdk, sandbox_id="cube:s1", mounts=mounts, command_timeout=600)


def test_execute_command_returns_stdout_and_exit_code(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("hi\n", "", 2)
    assert sandbox.execute_command("echo hi") == "hi\nexit code 2"


def test_command_wraps_output_and_uses_600s_timeout(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    seen = {}
    sandbox._sdk.commands.handler = lambda cmd, **kw: (seen.update(cmd=cmd, timeout=kw["timeout"]) or FakeCommandResult())
    sandbox.execute_command("echo hi")
    assert "head -c 1048576" in seen["cmd"]
    assert "PIPESTATUS" in seen["cmd"]
    assert seen["timeout"] == 600


def test_read_file_line_range(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "one\ntwo\nthree\n")
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt", 2, 2) == "two"


def test_write_file_creates_parent(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/sub/a.txt", "x")
    assert (tmp_path / "user-data" / "workspace" / "sub" / "a.txt").read_text() == "x"
    assert any("mkdir -p" in call["cmd"] for call in sandbox._sdk.commands.calls)


def test_download_and_update_bytes(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.update_file("/mnt/user-data/workspace/a.bin", b"\x00\x01")
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("AAE=")
    assert sandbox.download_file("/mnt/user-data/workspace/a.bin") == b"\x00\x01"


def test_file_tools_reject_escape_and_read_only(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(PermissionError):
        sandbox.read_file("/mnt/user-data/../skills/x")
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_sandbox.py -v. Expected: FAIL with missing CubeSandbox.

- [ ] **Step 3: Implement commands and file I/O**

CubeSandbox builds VirtualMount values from MountEntry, validates every file path with assert_virtual_path, and builds PathMapping values for mask_host_paths. _execute_command calls sdk.commands.run with the wrapped command, timeout or self.command_timeout, envs=env, user=self.sandbox_user, combines stdout/stderr, appends exit code N on failure, and maps HTTP 404/410/timeouts through the error helpers from client.py.

write_text calls ensure_parent_dir (a mkdir -p -- dirname command) and sdk.files.write. append=True reads the current text and writes it back under get_file_operation_lock(self, path). read_text uses sdk.files.read(path, user=self.sandbox_user) — real cubesandbox 0.7.3 has no format argument. download_file uses a bounded chunked base64 transfer through commands.run: stat the size, read 256 KiB slices with dd, pipe each through base64 -w0, decode, and concatenate; update_file writes bytes through sdk.files.write, which accepts str or bytes. FakeFiles may accept format for test convenience, but production code must not rely on it.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_sandbox.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/files.py   backend/packages/harness/agentkit/sandbox/cube/sandbox.py   backend/packages/harness/agentkit/sandbox/cube/__init__.py   backend/tests/test_cube_sandbox.py
git commit -m "feat(sandbox): execute commands and file I/O in CubeSandbox"
~~~

---

### Task 7: Tree Listing and Search

**Files:**
- Modify: backend/packages/harness/agentkit/sandbox/cube/files.py
- Modify: backend/packages/harness/agentkit/sandbox/cube/sandbox.py
- Test: backend/tests/test_cube_search.py

**Interfaces:**
- Produces: list_tree(sdk, path, *, user, max_depth=2) -> list[str].
- Produces: glob_paths(sdk, path, pattern, *, user, include_dirs, max_results) -> tuple[list[str], bool].
- Produces: grep_paths(sdk, path, pattern, *, user, glob=None, literal=False, case_sensitive=False, max_results=100) -> tuple[list[GrepMatch], bool].
- Produces: SEARCH_HELPER_PATH = "/tmp/.myflow/search.py" and ensure_search_helper(sdk, *, user) -> None.
- CubeSandbox.list_dir, glob, and grep delegate to these functions.

- [ ] **Step 1: Write the failing search tests**

~~~python
import json

import pytest

from agentkit.sandbox.cube.mounts import MountEntry
from agentkit.sandbox.cube.sandbox import CubeSandbox
from fakes.cube_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox


def _sandbox(tmp_path):
    sdk = FakeSdkSandbox(
        files=FakeFiles({"/mnt/user-data": tmp_path / "user-data"}),
        commands=FakeCommands(),
    )
    return CubeSandbox(
        sdk, sandbox_id="cube:s1",
        mounts=[MountEntry("/mnt/user-data", str(tmp_path / "user-data"), False)],
    )


def test_list_dir_renders_tree_and_empty(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "x")
    sandbox.write_file("/mnt/user-data/workspace/sub/b.txt", "y")
    lines = sandbox.list_dir("/mnt/user-data/workspace")
    assert lines == ["├── sub/", "│   └── b.txt", "└── a.txt"]


def test_list_dir_missing_is_file_not_found(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(FileNotFoundError):
        sandbox.list_dir("/mnt/user-data/missing")


def test_glob_parses_helper_json_and_truncation(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(
        json.dumps({"matches": ["/mnt/user-data/workspace/a.py"], "truncated": True})
    )
    matches, truncated = sandbox.glob("/mnt/user-data/workspace", "**/*.py")
    assert matches == ["/mnt/user-data/workspace/a.py"]
    assert truncated is True


def test_grep_parses_helper_json(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(
        json.dumps({"matches": [{"path": "/mnt/user-data/workspace/a.py", "line_number": 2, "line": "needle"}], "truncated": False})
    )
    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "needle", literal=True)
    assert matches[0].path.endswith("a.py")
    assert matches[0].line_number == 2
    assert truncated is False


def test_search_helper_is_written_once(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(json.dumps({"matches": [], "truncated": False}))
    sandbox.glob("/mnt/user-data/workspace", "*.py")
    sandbox.glob("/mnt/user-data/workspace", "*.py")
    assert sandbox._sdk.files.writes.count("/tmp/.myflow/search.py") == 1
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_search.py -v. Expected: FAIL with missing list_dir behavior or helper.

- [ ] **Step 3: Implement tree and search**

list_tree uses sdk.files.list(path, user=user); entries with type == "FILE_TYPE_DIRECTORY" are directories. Sort directories first, then case-insensitive names; emit the same tree symbols as agentkit.sandbox.local.list_dir.render_tree. Missing path raises FileNotFoundError; a file path raises NotADirectoryError; other failures raise OSError.

ensure_search_helper writes SEARCH_HELPER to /tmp/.myflow/search.py via sdk.files.write once per CubeSandbox instance. glob_paths and grep_paths call sdk.commands.run("python3 /tmp/.myflow/search.py <base64-json>", timeout=..., user=user), parse the JSON {"matches": ..., "truncated": bool}, and convert grep dicts to GrepMatch. The helper itself must reject absolute/escaping paths and cap results before serializing.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_search.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/files.py   backend/packages/harness/agentkit/sandbox/cube/sandbox.py backend/tests/test_cube_search.py
git commit -m "feat(sandbox): add CubeSandbox tree listing and search"
~~~

---

### Task 8: CubeSandboxProvider Lifecycle

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/cube/provider.py
- Modify: backend/packages/harness/agentkit/sandbox/cube/__init__.py
- Test: backend/tests/test_cube_provider.py

**Interfaces:**
- Produces: CubeSandboxProvider(*, app_config: AppConfig | None = None, sdk_factory: SdkFactory | None = None, lock: ThreadLock | None = None, paths: Paths | None = None, instance_id: str | None = None).
- Produces: startup_check() -> None, acquire(thread_id=None, *, user_id=None) -> str, get(sandbox_id) -> CubeSandbox | None, release(sandbox_id) -> None, release_async(sandbox_id) -> None, reset() -> None, shutdown() -> None, desired_mounts_hash(user_id, thread_id) -> str.
- Produces: supports_agent_skill_isolation = True; supports_per_user_skill_mounts returns True for git source and False for filesystem source; uses_thread_data_mounts = True.
- Sandbox id: cube:<remote_id>.
- Metadata: myflow=1, myflow.version=1, myflow.user, myflow.thread (__generic__ for none), myflow.generic, myflow.instance, myflow.mounts_hash, host-mount.
- Canary path: /mnt/user-data/workspace/.myflow-canary/in and .../out.

- [ ] **Step 1: Write the failing provider tests**

~~~python
import pytest

from agentkit.config import AppConfig, SandboxConfig, SkillsConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import (
    CubeSandboxConfig,
    CubeSandboxHostMountConfig,
    CubeSandboxLeaseConfig,
)
from agentkit.sandbox.cube.provider import CubeSandboxProvider
from fakes.cube_sdk import FakeSdkFactory


class FakeLock:
    def __init__(self): self.released = []; self.pings = 0
    def ping(self): self.pings += 1
    def acquire(self, key, owner, timeout): return None
    def renew(self, key, owner): return True
    def release(self, key, owner): self.released.append((key, owner))


def _provider(tmp_path):
    cube = CubeSandboxConfig(
        template_id="tpl",
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
    factory = FakeSdkFactory(root_map={"/mnt/user-data": tmp_path / ".myflow/users/u1/threads/t1/user-data"})
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
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_provider.py -v. Expected: FAIL with missing provider.

- [ ] **Step 3: Implement the provider**

Implement desired_mounts_hash(user_id, thread_id) by calling build_host_mounts and mounts_hash; acquire uses the same function. Use OrderedDict for the cache and a WeakValueDictionary (or a lock dict) for per-key in-process creation locks. acquire order is: validate ids -> cache hit with live handle and non-lost lease -> per-key lock -> cache re-check -> Redis lease acquire (wait acquire_timeout_seconds) -> discover by metadata -> compare mounts_hash -> connect or create -> canary -> cache -> start renewal. get never calls the factory. release stops renewal, executes pause or kill, releases the Redis lock in finally, removes the cache entry, and closes the SDK. reset clears cache and releases locks without pausing/killing. shutdown kills cached sandboxes, releases locks, and closes SDKs. startup_check calls factory.health() when configured, lock.ping(), and validate_mount_sources.

Canary: write a random token to the host workspace/.myflow-canary/in, read it through CubeSandbox.read_file; then write out through CubeSandbox.write_file and read it from the host. Clean both files/dir best-effort. On failure: kill the new sandbox, release the lock, raise SandboxRuntimeError("host mount canary failed: ...").

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_provider.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/cube/provider.py   backend/packages/harness/agentkit/sandbox/cube/__init__.py backend/tests/test_cube_provider.py
git commit -m "feat(sandbox): add CubeSandbox provider lifecycle"
~~~

---

### Task 9: Async Release and Gateway Startup

**Files:**
- Modify: backend/packages/harness/agentkit/sandbox/sandbox_provider.py
- Modify: backend/packages/harness/agentkit/sandbox/lease.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/sandbox_middleware.py
- Modify: backend/packages/harness/agentkit/sandbox/runtime.py
- Modify: backend/app/gateway/app.py
- Test: backend/tests/test_cube_runtime.py

**Interfaces:**
- Produces: SandboxProvider.startup_check() -> None default no-op.
- Produces: SandboxProvider.release_async(sandbox_id) -> None default asyncio.to_thread(self.release, sandbox_id).
- Produces: SandboxLeaseClient(sandbox_id, release, release_async); SandboxLeaseClient.release_now_async().
- Produces: SandboxLeaseManager.release_async(owner_id) -> bool awaits the holder's async release.
- Produces: start_sandbox_provider(*, app_config=None) -> SandboxProvider.
- Gateway lifespan calls start_sandbox_provider(app_config) after start_skill_runtime; shutdown calls reset_sandbox_provider() after run_manager.shutdown.
- SandboxMiddleware.aafter_agent awaits async release; sync after_agent remains sync.

- [ ] **Step 1: Write the failing runtime tests**

~~~python
import asyncio

from agentkit.sandbox import runtime


class FakeProvider:
    def __init__(self): self.checked = 0; self.released = []
    def startup_check(self): self.checked += 1
    async def release_async(self, sandbox_id): self.released.append(sandbox_id)
    def release(self, sandbox_id): self.released.append(sandbox_id)


def test_start_sandbox_provider_calls_startup_check(monkeypatch) -> None:
    fake = FakeProvider()
    monkeypatch.setattr(runtime, "get_sandbox_provider", lambda: fake)
    assert runtime.start_sandbox_provider() is fake
    assert fake.checked == 1


def test_provider_release_async_default_offloads() -> None:
    calls = []
    from agentkit.sandbox.sandbox_provider import SandboxProvider

    class Shim(SandboxProvider):
        def acquire(self, thread_id=None, *, user_id=None): return "s1"
        def get(self, sandbox_id): return None
        def release(self, sandbox_id): calls.append(sandbox_id)

    asyncio.run(Shim().release_async("s1"))
    assert calls == ["s1"]
~~~

Also add a lease test asserting SandboxLeaseManager.release_async calls FakeProvider.release_async rather than sync release when the last holder leaves.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_cube_runtime.py -v. Expected: FAIL with missing start_sandbox_provider or release_async.

- [ ] **Step 3: Implement the async release path and startup hook**

Add startup_check and release_async to the base. Extend SandboxLeaseClient with an async release callable defaulting to asyncio.to_thread(release); current_sandbox_lease passes the provider's release_async when present. Change SandboxLeaseManager.release_async to await that callable; keep release sync. Add _release_async in SandboxMiddleware and use it from aafter_agent; sync after_agent keeps _release. Add start_sandbox_provider to runtime.py.

In app.py, after await start_skill_runtime(...), call start_sandbox_provider(app_config). In the shutdown finally, after services.run_manager.shutdown(...), call reset_sandbox_provider(). Do not create a Cube sandbox at startup.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_cube_runtime.py tests/test_subagent_sandbox.py -v. Expected: PASS. Update existing lease tests only if the SandboxLeaseClient constructor change requires it; keep sync behavior.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/sandbox/sandbox_provider.py   backend/packages/harness/agentkit/sandbox/lease.py   backend/packages/harness/agentkit/agents/middlewares/sandbox_middleware.py   backend/packages/harness/agentkit/sandbox/runtime.py   backend/app/gateway/app.py backend/tests/test_cube_runtime.py
git commit -m "feat(sandbox): add async release and Cube startup check"
~~~

---

### Task 10: Configuration Docs and Live Integration

**Files:**
- Modify: config.example.yaml
- Modify: backend/README.md
- Test: backend/tests/test_cube_integration.py
- Test: backend/tests/test_app_config.py (only if the example config parser needs a new assertion)

**Interfaces:**
- config.example.yaml gets a commented sandbox.cube block matching the spec, so test_example_config_declares_m1_sections still parses.
- backend/README.md documents: same-host prerequisite, allowed_host_mount_prefixes, template dirs, agentkit[cube,redis], Redis URL, canary, and rollback to local.
- Live test reads CUBE_INTEGRATION=1 plus CUBE_API_URL, CUBE_TEMPLATE_ID, CUBE_PROXY_NODE_IP, and REDIS_URL; it skips otherwise.

- [ ] **Step 1: Write the opt-in integration test**

~~~python
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CUBE_INTEGRATION") != "1",
    reason="set CUBE_INTEGRATION=1 and provide a CubeSandbox cluster",
)


def test_cube_provider_round_trip(tmp_path) -> None:
    # Build AppConfig from the live config; acquire a sandbox for a temp thread.
    # Assert bash echo, read/write /mnt/user-data/workspace, canary, pause, and release.
    ...
~~~

The live test must not run on the Windows dev machine; it is for the Linux host.

- [ ] **Step 2: Run the offline suite**

Run: cd backend && uv run pytest tests/test_cube_config.py tests/test_cube_virtual_paths.py tests/test_cube_mounts.py tests/test_cube_locks.py tests/test_cube_client.py tests/test_cube_sandbox.py tests/test_cube_search.py tests/test_cube_provider.py tests/test_cube_runtime.py -v. Expected: PASS.

- [ ] **Step 3: Document and update the example config**

Add the commented config block, install command, CubeMaster allowed-prefix requirement, template requirements, and rollback steps. Do not enable Cube in config.example.yaml; keep it commented so the default local config still parses without Cube environment variables.

- [ ] **Step 4: Run the full backend suite and the opt-in live test**

Run: cd backend && uv run pytest. Expected: PASS.

Run on the Linux host: cd backend && CUBE_INTEGRATION=1 uv run pytest tests/test_cube_integration.py -v. Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add config.example.yaml backend/README.md backend/tests/test_cube_integration.py
git commit -m "docs(sandbox): document CubeSandbox host-mount deployment"
~~~

---

## Self-Review

**Spec coverage:** Tasks 1-10 cover config, path security, host mounts, Redis lock, SDK adapter, commands/files, tree/search, provider lifecycle/canary, async release/startup, docs/live test. thread_files.py is intentionally untouched; the spec's "no second workspace store" is preserved.

**Step scan:** Each task has a failing test, a run-to-fail command, one implementation step, a run-to-pass command, and a commit. The only multi-action step is Task 9's async plumbing, which is one coherent interface change.

**Type consistency:** MountEntry, HostRoots, VirtualMount, SdkSandbox, SdkFactory, ThreadLock, CubeSandbox, and CubeSandboxProvider names/signatures are introduced before use. CubeSandbox._sdk is the test seam; path_mappings is the masking seam.

**Review Focus:** The five risk items map to Task 8 canary, Task 4/8 lock loss, Task 8 mount hash, Task 2/6/7 path/read-only, and Task 6 output/timeout tests.

**Proportion:** The plan specifies interfaces and test contracts; it does not transcribe implementations. The live integration test is intentionally a manual gate because the cluster is not available on the Windows dev machine.
