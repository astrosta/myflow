"""E2BSandboxProvider: per-thread lifecycle over pure E2B sandboxes.

One provider owns one E2B sandbox per (user_id, thread_id).  There is no host
mount: the sandbox filesystem is authoritative, and files cross the boundary
through explicit sync (acquire uploads uploads/, release downloads outputs/).
Skills are uploaded lazily by E2BSandbox when a tool first reads them.

A Redis lease serializes discovery/create across Gateway workers, and an
in-process per-key lock keeps one worker from racing itself.  The provider never
treats a live SDK handle as proof that the lease is still ours.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shlex
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from agentkit.config import AppConfig, get_app_config
from agentkit.config.paths import Paths, get_paths
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.sandbox_config import E2BSandboxConfig
from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.sandbox.e2b.client import (
    E2BFactory,
    SdkFactory,
    SdkSandbox,
    is_gone_error,
)
from agentkit.sandbox.e2b.locks import LockLease, RedisThreadLock, ThreadLock
from agentkit.sandbox.e2b.sandbox import E2BSandbox
from agentkit.sandbox.e2b.sync import (
    SkillSource,
    SyncLimits,
    download_tree,
    ensure_container_dirs,
    upload_tree,
)
from agentkit.sandbox.exceptions import SandboxError, SandboxRuntimeError
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.sandbox.security import PathMapping
from agentkit.skills.builtin import resolve_builtin_root
from agentkit.utils.ids import validate_thread_id, validate_user_id

logger = logging.getLogger(__name__)

_SANDBOX_PREFIX = "e2b:"
_GENERIC_THREAD = "__generic__"
_USER_DATA_CONTAINER = "/mnt/user-data"
_CANARY_DIR = ".myflow-canary"
_CANARY_FILE = "canary"
_SANDBOX_DIR_NAMES = ("workspace", "uploads", "outputs")


@dataclass
class _E2BHandle:
    """One cached remote sandbox plus the lease that makes it ours."""

    key: tuple[str, str | None]
    remote_id: str
    sandbox_id: str
    sdk: SdkSandbox
    sandbox: E2BSandbox
    lease: LockLease
    user: str
    thread: str | None
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    alive: bool = True


class E2BSandboxProvider(SandboxProvider):
    """SandboxProvider backed by E2B sandboxes and explicit file sync."""

    uses_thread_data_mounts = True
    supports_agent_skill_isolation = True

    def __init__(
        self,
        *,
        app_config: AppConfig | None = None,
        sdk_factory: SdkFactory | None = None,
        lock: ThreadLock | None = None,
        paths: Paths | None = None,
        instance_id: str | None = None,
    ) -> None:
        config = app_config if app_config is not None else get_app_config()
        e2b = config.sandbox.e2b
        if e2b is None:
            raise SandboxRuntimeError(
                "sandbox.e2b is required for agentkit.sandbox.e2b:E2BSandboxProvider"
            )
        if not e2b.template_id.strip():
            raise SandboxRuntimeError("sandbox.e2b.template_id must not be blank")

        self._app_config = config
        self._e2b: E2BSandboxConfig = e2b
        self._paths = paths if paths is not None else get_paths()
        self._instance_id = instance_id or uuid.uuid4().hex
        self._sdk_factory: SdkFactory = sdk_factory or E2BFactory(
            api_url=e2b.api_url,
            api_key=e2b.api_key,
            template_id=e2b.template_id,
            sandbox_domain=e2b.sandbox_domain,
            request_timeout=e2b.request_timeout_seconds,
        )
        if lock is None:
            try:
                lock = RedisThreadLock(e2b.lease.redis_url, e2b.lease.ttl_seconds)
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "E2B leases need the redis extra: install agentkit[e2b,redis]"
                ) from exc
        self._lock: ThreadLock = lock
        self._max_cached_threads = max(1, int(e2b.max_cached_threads))

        self._cache: OrderedDict[tuple[str, str | None], _E2BHandle] = OrderedDict()
        self._by_remote_id: dict[str, _E2BHandle] = {}
        self._cache_lock = threading.RLock()
        self._key_locks_guard = threading.Lock()
        self._key_locks: dict[tuple[str, str | None], threading.Lock] = {}

    # -- capability flags -------------------------------------------------

    @property
    def supports_per_user_skill_mounts(self) -> bool:
        """Only the git catalog materializes a private active tree per user."""

        return self._app_config.skills.source == "git"

    # -- startup ----------------------------------------------------------

    def startup_check(self) -> None:
        """Fail startup on an unreachable E2B control plane or Redis."""

        if self._e2b.startup_healthcheck:
            try:
                self._sdk_factory.health()
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "E2B provider needs the e2b extra: install agentkit[e2b,redis]"
                ) from exc
        if self._lock.ping() is False:
            raise SandboxRuntimeError(
                "E2B lease Redis PING failed; sandbox.e2b.lease.redis_url "
                "must point at a reachable Redis"
            )

    # -- lifecycle --------------------------------------------------------

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        user = validate_user_id(user_id or DEFAULT_USER_ID)
        thread = self._validate_thread(thread_id)
        key = (user, thread)

        cached = self._live_cached(key)
        if cached is not None:
            return cached.sandbox_id

        with self._key_lock_for(key):
            # Another caller in this process may have populated the cache while
            # we waited for the per-key lock.
            cached = self._take_live_cached(key)
            if cached is not None:
                return cached.sandbox_id
            return self._acquire_locked(key, user, thread)

    def get(self, sandbox_id: str) -> E2BSandbox | None:
        """Pure memory lookup. No health check, no SDK call."""

        remote_id = self._remote_id_from_sandbox_id(sandbox_id)
        if remote_id is None:
            return None
        with self._cache_lock:
            handle = self._by_remote_id.get(remote_id)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(handle.key)
            return handle.sandbox

    def release(self, sandbox_id: str) -> None:
        """Download outputs, then pause or kill one owned sandbox."""

        handle = self._take_handle(sandbox_id)
        if handle is None:
            return
        try:
            if not handle.lease.lost:
                self._download_outputs(handle)
                try:
                    self._apply_action(handle.sdk, self._e2b.release_action)
                except Exception:
                    logger.warning(
                        "E2B sandbox %s %s failed; releasing the lease anyway",
                        handle.sandbox_id,
                        self._e2b.release_action,
                        exc_info=True,
                    )
        finally:
            self._release_lease(handle)
            self._close_sdk(handle.sdk)

    async def release_async(self, sandbox_id: str) -> None:
        """Remote sync and pause/kill must not run on the event loop."""

        await asyncio.to_thread(self.release, sandbox_id)

    def reset(self) -> None:
        """Drop local handles and leases. Remote sandboxes keep running."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            self._release_lease(handle)
            self._close_sdk(handle.sdk)

    def shutdown(self) -> None:
        """Download outputs, kill every sandbox this provider still owns, clear state."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            try:
                self._download_outputs(handle)
                try:
                    self._apply_action(handle.sdk, "kill")
                except Exception:
                    logger.warning(
                        "E2B sandbox %s kill failed during shutdown",
                        handle.sandbox_id,
                        exc_info=True,
                    )
            finally:
                self._release_lease(handle)
                self._close_sdk(handle.sdk)

    # -- acquire internals ------------------------------------------------

    def _acquire_locked(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
    ) -> str:
        stale = self._drop_stale_locked(key)
        if stale is not None:
            # A handle whose lease was lost is not ours to pause or kill.
            self._release_lease(stale)
            self._close_sdk(stale.sdk)

        owner = f"{self._instance_id}:{uuid.uuid4().hex}"
        lease = LockLease(
            self._lock,
            self._lock_key(user, thread),
            owner,
            self._e2b.lease.ttl_seconds,
            on_lost=lambda: self._handle_lease_lost(key),
        )

        lease_acquired = False
        cached = False
        sdk: SdkSandbox | None = None
        created = False
        try:
            lease.acquire(self._e2b.acquire_timeout_seconds)
            lease_acquired = True

            sdk, created = self._discover_or_create(user, thread)
            sandbox = self._build_sandbox(
                sdk,
                user=user,
                thread=thread,
                sandbox_id=f"{_SANDBOX_PREFIX}{sdk.sandbox_id}",
            )

            try:
                self._prepare_sandbox(sdk, user=user, thread=thread)
                self._run_canary(sandbox)
            except SandboxError:
                self._destroy_sdk(sdk, kill=created)
                sdk = None
                created = False
                raise
            except Exception as exc:
                self._destroy_sdk(sdk, kill=created)
                sdk = None
                created = False
                raise SandboxRuntimeError(f"E2B sandbox acquire failed: {exc}") from exc

            handle = _E2BHandle(
                key=key,
                remote_id=sdk.sandbox_id,
                sandbox_id=sandbox.id,
                sdk=sdk,
                sandbox=sandbox,
                lease=lease,
                user=user,
                thread=thread,
            )
            self._store_handle(handle)
            cached = True
            try:
                lease.start()
            except Exception:
                self._remove_handle(handle)
                cached = False
                raise
            return sandbox.id
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxRuntimeError(f"E2B sandbox acquire failed: {exc}") from exc
        finally:
            if not cached:
                if sdk is not None:
                    if created:
                        self._kill_quietly(sdk)
                    self._close_sdk(sdk)
                if lease_acquired:
                    self._release_lease_from(lease)

    def _build_sandbox(
        self,
        sdk: SdkSandbox,
        *,
        user: str,
        thread: str | None,
        sandbox_id: str,
    ) -> E2BSandbox:
        sync = self._e2b.sync
        sources = (
            self._skill_sources(user)
            if sync.enabled and sync.upload_skills
            else ()
        )
        return E2BSandbox(
            sdk,
            sandbox_id=sandbox_id,
            path_mappings=self._path_mappings(user=user, thread=thread, sources=sources),
            skill_sources=sources,
            sync_limits=self._sync_limits(),
            sandbox_user=self._e2b.sandbox_user,
            command_timeout=float(self._app_config.sandbox.bash_command_timeout),
            # A 404/410 means the sandbox is gone: drop the handle so the
            # runtime re-acquires instead of re-serving a dead sandbox.
            on_dead=partial(self._handle_reaped, sdk.sandbox_id),
        )

    def _prepare_sandbox(
        self, sdk: SdkSandbox, *, user: str, thread: str | None
    ) -> None:
        """Create the sandbox directory tree and upload the host uploads/ tree."""

        user_data = self._ensure_host_user_data(user, thread)
        ensure_container_dirs(
            sdk,
            [
                f"{_USER_DATA_CONTAINER}/workspace",
                f"{_USER_DATA_CONTAINER}/uploads",
                f"{_USER_DATA_CONTAINER}/outputs",
            ],
            user=self._e2b.sandbox_user,
        )
        sync = self._e2b.sync
        if not sync.enabled:
            return
        limits = self._sync_limits()
        if sync.upload_uploads:
            self._log_stats(
                "uploads",
                upload_tree(
                    sdk,
                    host_root=user_data / "uploads",
                    container_root=f"{_USER_DATA_CONTAINER}/uploads",
                    limits=limits,
                    user=self._e2b.sandbox_user,
                    label="uploads",
                ),
            )
        if sync.upload_workspace:
            self._log_stats(
                "workspace",
                upload_tree(
                    sdk,
                    host_root=user_data / "workspace",
                    container_root=f"{_USER_DATA_CONTAINER}/workspace",
                    limits=limits,
                    user=self._e2b.sandbox_user,
                    label="workspace",
                ),
            )

    def _download_outputs(self, handle: _E2BHandle) -> None:
        sync = self._e2b.sync
        if not sync.enabled or not sync.download_outputs:
            return
        try:
            user_data = self._ensure_host_user_data(handle.user, handle.thread)
            stats = download_tree(
                handle.sdk,
                host_root=user_data / "outputs",
                container_root=f"{_USER_DATA_CONTAINER}/outputs",
                limits=self._sync_limits(),
                user=self._e2b.sandbox_user,
                label="outputs",
            )
            self._log_stats("outputs", stats)
            if sync.download_workspace:
                workspace_stats = download_tree(
                    handle.sdk,
                    host_root=user_data / "workspace",
                    container_root=f"{_USER_DATA_CONTAINER}/workspace",
                    limits=self._sync_limits(),
                    user=self._e2b.sandbox_user,
                    label="workspace",
                )
                self._log_stats("workspace", workspace_stats)
        except Exception:
            logger.warning(
                "could not download E2B outputs for %s; keeping the sandbox",
                handle.sandbox_id,
                exc_info=True,
            )

    @staticmethod
    def _log_stats(label: str, stats: Any) -> None:
        if stats.files or stats.skipped_files or stats.skipped_trees:
            logger.info(
                "E2B sync %s: files=%d bytes=%d skipped_files=%d skipped_trees=%d",
                label,
                stats.files,
                stats.bytes,
                stats.skipped_files,
                stats.skipped_trees,
            )

    def _discover_or_create(
        self,
        user: str,
        thread: str | None,
    ) -> tuple[SdkSandbox, bool]:
        rows = self._discover(user, thread)
        chosen = rows[0] if rows else None
        for duplicate in rows[1:]:
            metadata = self._row_metadata(duplicate)
            if metadata.get("myflow.instance") == self._instance_id:
                self._kill_remote(self._require_remote_id(duplicate))
            else:
                logger.warning(
                    "multiple E2B sandboxes match user=%s thread=%s; keeping %s",
                    user,
                    thread,
                    self._require_remote_id(chosen) if chosen is not None else None,
                )

        if chosen is not None:
            remote_id = self._require_remote_id(chosen)
            try:
                return self._sdk_factory.connect(remote_id), False
            except Exception as exc:
                raise SandboxRuntimeError(
                    f"E2B sandbox connect failed for {remote_id}: {exc}"
                ) from exc

        return self._create_sandbox(user, thread), True

    def _discover(self, user: str, thread: str | None) -> list[dict]:
        query = {
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
        }
        try:
            rows = self._sdk_factory.list_by_metadata(query)
        except Exception as exc:
            raise SandboxRuntimeError(f"E2B sandbox metadata discovery failed: {exc}") from exc
        if not isinstance(rows, list):
            raise SandboxRuntimeError("E2B sandbox metadata discovery returned non-list")

        matches: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            metadata = self._row_metadata(row)
            if str(metadata.get("myflow.user")) != user:
                continue
            if str(metadata.get("myflow.thread")) != (thread or _GENERIC_THREAD):
                continue
            if self._remote_id_from_row(row) is None:
                continue
            matches.append(row)
        # The API usually returns creation order, but the choice of instance is
        # part of the contract: always keep the earliest one when it does not.
        matches.sort(key=self._creation_order)
        return matches

    def _create_sandbox(self, user: str, thread: str | None) -> SdkSandbox:
        metadata = self._metadata(user, thread)
        network: dict[str, Any] = {
            "allow_public_traffic": self._e2b.allow_public_traffic,
            "allow_out": list(self._e2b.network.allow_out),
            "deny_out": list(self._e2b.network.deny_out),
        }
        if self._e2b.network.rules is not None:
            network["rules"] = self._e2b.network.rules
        try:
            return self._sdk_factory.create(
                template=self._e2b.template_id,
                timeout=self._e2b.timeout_seconds,
                metadata=metadata,
                lifecycle={
                    "on_timeout": self._e2b.on_timeout,
                    "auto_resume": self._e2b.auto_resume,
                },
                network=network,
                allow_internet_access=self._e2b.allow_internet_access,
            )
        except Exception as exc:
            raise SandboxRuntimeError(f"E2B sandbox create failed: {exc}") from exc

    def _metadata(self, user: str, thread: str | None) -> dict[str, str]:
        return {
            "myflow.version": "1",
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
            "myflow.instance": self._instance_id,
        }

    # -- canary -----------------------------------------------------------

    def _run_canary(self, sandbox: E2BSandbox) -> None:
        """Prove the file API and the container view are the same file.

        Write through files.write, read back through files.read, then read the
        same path with cat inside the sandbox.  With no host mount, this
        three-way agreement is the only invariant worth checking at acquire.
        """

        canary_dir = f"{_USER_DATA_CONTAINER}/workspace/{_CANARY_DIR}"
        canary_path = f"{canary_dir}/{_CANARY_FILE}"
        token = secrets.token_hex(16)
        try:
            sandbox.update_file(canary_path, token.encode("utf-8"))
            observed = sandbox.download_file(canary_path)
            if observed.decode("utf-8", errors="replace").strip() != token:
                raise SandboxRuntimeError(
                    "the file API read back a different canary token"
                )
            output = sandbox.execute_command(
                f"cat -- {shlex.quote(canary_path)}"
            ).strip()
            if output != token:
                raise SandboxRuntimeError(
                    "cat inside the sandbox did not see the file-API token"
                )
        except Exception as exc:
            raise SandboxRuntimeError(
                "E2B sandbox canary failed: "
                f"{exc}. Check sandbox.e2b.api_key, the template's mkdir/cat/python3 "
                "tools, and that files.write and files.read reach the same sandbox."
            ) from exc
        finally:
            try:
                sandbox.execute_command(
                    f"rm -f -- {shlex.quote(canary_path)}; "
                    f"rmdir -- {shlex.quote(canary_dir)} 2>/dev/null || true"
                )
            except Exception:
                logger.warning("could not remove the E2B canary probe", exc_info=True)

    # -- cache and leases -------------------------------------------------

    def _live_cached(self, key: tuple[str, str | None]) -> _E2BHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _take_live_cached(self, key: tuple[str, str | None]) -> _E2BHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                # Leave a stale handle in place for _drop_stale_locked, which
                # disposes of its lease outside the cache lock.
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _drop_stale_locked(self, key: tuple[str, str | None]) -> _E2BHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            if handle.alive and not handle.lease.lost:
                return None
            self._remove_locked(handle)
            return handle

    def _store_handle(self, handle: _E2BHandle) -> None:
        evicted: list[_E2BHandle] = []
        with self._cache_lock:
            while len(self._cache) >= self._max_cached_threads:
                _, old = self._cache.popitem(last=False)
                old.alive = False
                self._by_remote_id.pop(old.remote_id, None)
                evicted.append(old)
            self._cache[handle.key] = handle
            self._cache.move_to_end(handle.key)
            self._by_remote_id[handle.remote_id] = handle

        for old in evicted:
            # Eviction only frees a process-local slot. The remote sandbox may be
            # in use by another run, so it is neither paused nor killed.
            self._release_lease(old)
            self._close_sdk(old.sdk)

    def _take_handle(self, sandbox_id: str) -> _E2BHandle | None:
        remote_id = self._remote_id_from_sandbox_id(sandbox_id)
        if remote_id is None:
            return None
        return self._take_handle_by_remote_id(remote_id)

    def _take_handle_by_remote_id(self, remote_id: str) -> _E2BHandle | None:
        """Remove one handle from the cache and from the remote index.

        The cache entry is dropped only while it still holds this handle: a
        concurrent acquire may already have stored a newer one for the key.
        """

        with self._cache_lock:
            handle = self._by_remote_id.pop(remote_id, None)
            if handle is None:
                return None
            handle.alive = False
            if self._cache.get(handle.key) is handle:
                self._cache.pop(handle.key, None)
        return handle

    def _remove_handle(self, handle: _E2BHandle) -> None:
        with self._cache_lock:
            self._remove_locked(handle)

    def _remove_locked(self, handle: _E2BHandle) -> None:
        handle.alive = False
        self._cache.pop(handle.key, None)
        self._by_remote_id.pop(handle.remote_id, None)

    def _drain_cache(self) -> list[_E2BHandle]:
        with self._cache_lock:
            handles = list(self._cache.values())
            self._cache.clear()
            self._by_remote_id.clear()
        return handles

    def _handle_lease_lost(self, key: tuple[str, str | None]) -> None:
        """Detach a lease-lost handle without touching the remote sandbox.

        Another worker may already own the thread. The old handle must stop
        serving new calls so the runtime re-acquires and re-locks.
        """

        with self._cache_lock:
            handle = self._cache.pop(key, None)
            if handle is None:
                return
            handle.alive = False
            self._by_remote_id.pop(handle.remote_id, None)
        logger.warning(
            "E2B sandbox %s lost its Redis lease; dropping the local handle",
            handle.sandbox_id,
        )

    def _handle_reaped(self, remote_id: str) -> None:
        """Detach a handle whose remote sandbox answered 404/410.

        The sandbox is gone, so neither pause nor kill would help: both are only
        a wasted control-plane call that fails the same way. Releasing the lease
        is what lets the next acquire re-lock the thread and build a fresh one.
        """

        handle = self._take_handle_by_remote_id(remote_id)
        if handle is None:
            return
        logger.warning(
            "E2B sandbox %s no longer exists; dropping the handle for re-acquire",
            handle.sandbox_id,
        )
        self._release_lease(handle)
        self._close_sdk(handle.sdk)

    @staticmethod
    def _release_lease(handle: _E2BHandle) -> None:
        E2BSandboxProvider._release_lease_from(handle.lease)

    @staticmethod
    def _release_lease_from(lease: LockLease) -> None:
        try:
            lease.release()
        except Exception:
            logger.warning("could not release E2B Redis lease", exc_info=True)

    def _key_lock_for(self, key: tuple[str, str | None]) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # -- host paths and skills -------------------------------------------

    def _sync_limits(self) -> SyncLimits:
        sync = self._e2b.sync
        return SyncLimits(
            max_file_bytes=sync.max_file_bytes,
            max_tree_bytes=sync.max_tree_bytes,
        )

    def _host_user_data(self, user: str, thread: str | None) -> Path:
        if thread is None:
            return self._paths.base_dir / "users" / user / "local" / "user-data"
        return self._paths.sandbox_user_data_dir(thread, user_id=user)

    def _ensure_host_user_data(self, user: str, thread: str | None) -> Path:
        if thread is not None:
            self._paths.ensure_thread_dirs(thread, user_id=user)
            return self._paths.sandbox_user_data_dir(thread, user_id=user)
        root = self._host_user_data(user, thread)
        for name in _SANDBOX_DIR_NAMES:
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def _path_mappings(
        self,
        *,
        user: str,
        thread: str | None,
        sources: Sequence[SkillSource],
    ) -> list[PathMapping]:
        mappings: dict[str, PathMapping] = {
            _USER_DATA_CONTAINER: PathMapping(
                _USER_DATA_CONTAINER,
                str(self._host_user_data(user, thread)),
                False,
            )
        }
        if sources:
            skills_container = self._skills_container()
            mappings.setdefault(
                skills_container,
                PathMapping(skills_container, str(self._skills_mask_root(user, sources)), True),
            )
            for source in sources:
                mappings[source.container_root] = PathMapping(
                    source.container_root,
                    str(source.host_root),
                    True,
                )
        return list(mappings.values())

    def _skill_sources(self, user: str) -> tuple[SkillSource, ...]:
        skills = self._app_config.skills
        container = self._skills_container()
        sources: list[SkillSource] = []
        filesystem_root: Path | None = None
        if skills.source == "git":
            private_root = resolve_path(skills.git.cache_dir) / "active" / "users" / user
            sources.append(SkillSource(private_root, f"{container}/private"))
        else:
            filesystem_root = skills.root_dir()
            if filesystem_root.is_dir():
                sources.append(SkillSource(filesystem_root, container))
            else:
                logger.warning(
                    "filesystem skills root is missing; skill reads will stay empty: %s",
                    filesystem_root,
                )
        builtin_root = self._builtin_root()
        if builtin_root is not None and builtin_root.is_dir():
            if filesystem_root is not None and builtin_root.is_relative_to(filesystem_root):
                pass
            else:
                sources.append(SkillSource(builtin_root, f"{container}/builtin"))
        return tuple(sources)

    def _skills_container(self) -> str:
        return _parse_container_path(
            self._app_config.skills.container_path, "skills.container_path"
        )

    def _skills_mask_root(self, user: str, sources: Sequence[SkillSource]) -> Path:
        if sources:
            return sources[0].host_root
        return self._app_config.skills.root_dir()

    def _builtin_root(self) -> Path | None:
        skills = self._app_config.skills
        if not skills.builtin.enabled:
            return None
        try:
            return resolve_builtin_root(skills)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("built-in skills root is unusable: %s", exc)
            return None

    # -- small helpers ----------------------------------------------------

    @staticmethod
    def _validate_thread(thread_id: str | None) -> str | None:
        if thread_id is None:
            return None
        return validate_thread_id(thread_id)

    @staticmethod
    def _lock_key(user: str, thread: str | None) -> str:
        return f"myflow:sandbox:{user}:{thread or _GENERIC_THREAD}"

    @staticmethod
    def _remote_id_from_sandbox_id(sandbox_id: str) -> str | None:
        if not isinstance(sandbox_id, str) or not sandbox_id.startswith(_SANDBOX_PREFIX):
            return None
        remote_id = sandbox_id[len(_SANDBOX_PREFIX) :]
        return remote_id or None

    @staticmethod
    def _row_metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = row.get("metadata")
        return metadata if isinstance(metadata, Mapping) else {}

    @staticmethod
    def _creation_order(row: Mapping[str, Any]) -> tuple[int, str]:
        for field_name in ("createdAt", "created_at", "createTime", "created"):
            value = row.get(field_name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return (0, f"{float(value):020.6f}")
            if isinstance(value, str) and value:
                return (0, value)
        # Rows without a timestamp keep the API's order (stable sort).
        return (1, "")

    @staticmethod
    def _remote_id_from_row(row: Mapping[str, Any]) -> str | None:
        for field_name in ("sandboxID", "sandboxId", "sandbox_id", "id"):
            value = row.get(field_name)
            if isinstance(value, str) and value:
                return value
        return None

    def _require_remote_id(self, row: Mapping[str, Any]) -> str:
        remote_id = self._remote_id_from_row(row)
        if remote_id is None:
            raise SandboxRuntimeError("E2B sandbox metadata row has no sandbox id")
        return remote_id

    def _apply_action(self, sdk: SdkSandbox, action: str) -> None:
        try:
            if action == "kill":
                sdk.kill()
            elif action == "pause":
                sdk.pause()
            else:
                raise SandboxRuntimeError(f"unknown E2B release action: {action}")
        except Exception as exc:
            if is_gone_error(exc):
                return
            raise

    def _kill_remote(self, remote_id: str) -> None:
        try:
            sdk = self._sdk_factory.connect(remote_id)
        except Exception as exc:
            raise SandboxRuntimeError(
                f"E2B connect to stale instance {remote_id} failed: {exc}"
            ) from exc
        try:
            sdk.kill()
        except Exception as exc:
            if not is_gone_error(exc):
                raise SandboxRuntimeError(
                    f"E2B kill of stale instance {remote_id} failed: {exc}"
                ) from exc
        finally:
            self._close_sdk(sdk)

    def _destroy_sdk(self, sdk: SdkSandbox, *, kill: bool) -> None:
        if kill:
            self._kill_quietly(sdk)
        self._close_sdk(sdk)

    @staticmethod
    def _kill_quietly(sdk: SdkSandbox) -> None:
        try:
            sdk.kill()
        except Exception as exc:
            if not is_gone_error(exc):
                logger.warning("could not kill a failed E2B sandbox", exc_info=True)

    @staticmethod
    def _close_sdk(sdk: SdkSandbox) -> None:
        try:
            sdk.close()
        except Exception:
            logger.warning("could not close an E2B SDK handle", exc_info=True)


def _parse_container_path(value: str, field: str) -> str:
    """Validate skills.container_path without importing host-mount planning."""

    from pathlib import PurePosixPath

    text = str(value).strip()
    if not text:
        raise SandboxRuntimeError(f"{field} must not be blank")
    path = PurePosixPath(text)
    if not path.is_absolute():
        raise SandboxRuntimeError(f"{field} must be an absolute POSIX path: {text}")
    if path.parent == path:
        raise SandboxRuntimeError(f"{field} must not be the filesystem root: {text}")
    if any(part == ".." for part in path.parts):
        raise SandboxRuntimeError(f"{field} must not contain '..': {text}")
    return str(path)


__all__ = ["E2BSandboxProvider"]
