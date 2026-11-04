"""CubeSandboxProvider: per-thread lifecycle over CubeSandbox host mounts.

One provider owns one CubeSandbox per (user_id, thread_id). The host mount is the
workspace contract: a sandbox is useful only while its metadata mount fingerprint
matches the mounts this Gateway would build now. A Redis lease serializes
discovery/create across Gateway workers, and an in-process per-key lock keeps one
worker from racing itself. The provider never treats a live SDK handle as proof
that the lease is still ours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
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
from agentkit.config.sandbox_config import CubeSandboxConfig
from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.sandbox.cube.client import (
    E2BFactory,
    SdkFactory,
    SdkSandbox,
    is_gone_error,
)
from agentkit.sandbox.cube.locks import LockLease, RedisThreadLock, ThreadLock
from agentkit.sandbox.cube.mounts import (
    MountEntry,
    build_host_mounts,
    mounts_hash,
    resolve_host_roots,
    validate_mount_sources,
)
from agentkit.sandbox.cube.sandbox import CubeSandbox
from agentkit.sandbox.exceptions import SandboxError, SandboxRuntimeError
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.utils.ids import validate_thread_id, validate_user_id

logger = logging.getLogger(__name__)

_SANDBOX_PREFIX = "cube:"
_GENERIC_THREAD = "__generic__"
_USER_DATA_CONTAINER = "/mnt/user-data"
_CANARY_DIR = ".myflow-canary"
_CANARY_CONTAINER = f"{_USER_DATA_CONTAINER}/workspace/{_CANARY_DIR}"
_CANARY_IN = "in"
_CANARY_OUT = "out"


@dataclass
class _CubeHandle:
    """One cached remote sandbox plus the lease that makes it ours."""

    key: tuple[str, str | None]
    remote_id: str
    sandbox_id: str
    sdk: SdkSandbox
    sandbox: CubeSandbox
    lease: LockLease
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    alive: bool = True


class CubeSandboxProvider(SandboxProvider):
    """SandboxProvider backed by CubeSandbox MicroVMs and host mounts."""

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
        cube = config.sandbox.cube
        if cube is None:
            raise SandboxRuntimeError(
                "sandbox.cube is required for agentkit.sandbox.cube:CubeSandboxProvider"
            )
        if not cube.template_id.strip():
            raise SandboxRuntimeError("sandbox.cube.template_id must not be blank")

        # Validate the configured host root shape now. Mount sources are checked
        # in startup_check, because constructing the provider must not do IO.
        resolve_host_roots(cube)

        self._app_config = config
        self._cube: CubeSandboxConfig = cube
        self._paths = paths if paths is not None else get_paths()
        self._instance_id = instance_id or uuid.uuid4().hex
        self._sdk_factory: SdkFactory = sdk_factory or E2BFactory(
            api_url=cube.api_url,
            api_key=cube.api_key,
            template_id=cube.template_id,
            sandbox_domain=cube.sandbox_domain,
            request_timeout=cube.request_timeout_seconds,
        )
        if lock is None:
            try:
                lock = RedisThreadLock(cube.lease.redis_url, cube.lease.ttl_seconds)
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "CubeSandbox leases need the redis extra: install agentkit[cube,redis]"
                ) from exc
        self._lock: ThreadLock = lock
        self._max_cached_threads = max(1, int(cube.max_cached_threads))

        self._cache: OrderedDict[tuple[str, str | None], _CubeHandle] = OrderedDict()
        self._by_remote_id: dict[str, _CubeHandle] = {}
        self._cache_lock = threading.RLock()
        self._key_locks_guard = threading.Lock()
        self._key_locks: dict[tuple[str, str | None], threading.Lock] = {}

    # -- capability flags -------------------------------------------------

    @property
    def supports_per_user_skill_mounts(self) -> bool:
        """Only the git catalog materializes a private active tree per user."""

        return self._app_config.skills.source == "git"

    # -- startup and mount fingerprint -----------------------------------

    def startup_check(self) -> None:
        """Fail startup on an unreachable Cube/Redis or an invalid mount source."""

        if self._cube.startup_healthcheck:
            try:
                self._sdk_factory.health()
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "CubeSandbox needs the cube extra: install agentkit[cube,redis]"
                ) from exc
        if self._lock.ping() is False:
            raise SandboxRuntimeError(
                "CubeSandbox lease Redis PING failed; sandbox.cube.lease.redis_url "
                "must point at a reachable Redis"
            )
        validate_mount_sources(self._app_config, resolve_host_roots(self._cube))

    def desired_mounts_hash(self, user_id: str | None, thread_id: str | None) -> str:
        """Fingerprint the host mounts this provider would create right now."""

        user = validate_user_id(user_id or DEFAULT_USER_ID)
        thread = self._validate_thread(thread_id)
        mounts = build_host_mounts(
            app_config=self._app_config,
            user_id=user,
            thread_id=thread,
            paths=self._paths,
        )
        return mounts_hash(mounts)

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

    def get(self, sandbox_id: str) -> CubeSandbox | None:
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
        """Pause or kill one owned sandbox, then release its lease."""

        handle = self._take_handle(sandbox_id)
        if handle is None:
            return
        try:
            if not handle.lease.lost:
                self._apply_action(handle.sdk, self._cube.release_action)
        except Exception:
            logger.warning(
                "CubeSandbox %s %s failed; releasing the lease anyway",
                handle.sandbox_id,
                self._cube.release_action,
                exc_info=True,
            )
        finally:
            self._release_lease(handle)
            self._close_sdk(handle.sdk)

    async def release_async(self, sandbox_id: str) -> None:
        """Remote pause/kill must not run on the event loop."""

        await asyncio.to_thread(self.release, sandbox_id)

    def reset(self) -> None:
        """Drop local handles and leases. Remote sandboxes keep running."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            self._release_lease(handle)
            self._close_sdk(handle.sdk)

    def shutdown(self) -> None:
        """Kill every sandbox this provider still owns, then clear state."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            try:
                self._apply_action(handle.sdk, "kill")
            except Exception:
                logger.warning(
                    "CubeSandbox %s kill failed during shutdown",
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
            self._cube.lease.ttl_seconds,
            on_lost=lambda: self._handle_lease_lost(key),
        )

        lease_acquired = False
        cached = False
        sdk: SdkSandbox | None = None
        created = False
        try:
            lease.acquire(self._cube.acquire_timeout_seconds)
            lease_acquired = True

            mounts = build_host_mounts(
                app_config=self._app_config,
                user_id=user,
                thread_id=thread,
                paths=self._paths,
            )
            mount_hash = mounts_hash(mounts)
            sdk, created = self._discover_or_create(
                user, thread, mounts=mounts, mount_hash=mount_hash
            )
            sandbox = CubeSandbox(
                sdk,
                sandbox_id=f"{_SANDBOX_PREFIX}{sdk.sandbox_id}",
                mounts=mounts,
                sandbox_user=self._cube.sandbox_user,
                command_timeout=float(self._app_config.sandbox.bash_command_timeout),
                # A 404/410 means the MicroVM is gone: drop the handle so the
                # runtime re-acquires instead of re-serving a dead sandbox.
                on_dead=partial(self._handle_reaped, sdk.sandbox_id),
            )

            try:
                self._run_canary(sandbox, user=user, thread=thread)
            except Exception as exc:
                self._destroy_sdk(sdk, kill=created)
                sdk = None
                created = False
                raise SandboxRuntimeError(
                    "host mount canary failed: "
                    f"{exc}. Check sandbox.cube.host_mount, the template umask "
                    "(recommended 0022), and sandbox_user/host directory permissions."
                ) from exc

            handle = _CubeHandle(
                key=key,
                remote_id=sdk.sandbox_id,
                sandbox_id=sandbox.id,
                sdk=sdk,
                sandbox=sandbox,
                lease=lease,
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
            raise SandboxRuntimeError(f"CubeSandbox acquire failed: {exc}") from exc
        finally:
            if not cached:
                if sdk is not None:
                    if created:
                        self._kill_quietly(sdk)
                    self._close_sdk(sdk)
                if lease_acquired:
                    self._release_lease_from(lease)

    def _discover_or_create(
        self,
        user: str,
        thread: str | None,
        *,
        mounts: Sequence[MountEntry],
        mount_hash: str,
    ) -> tuple[SdkSandbox, bool]:
        rows = self._discover(user, thread)
        chosen = rows[0] if rows else None
        for duplicate in rows[1:]:
            metadata = self._row_metadata(duplicate)
            if metadata.get("myflow.instance") == self._instance_id:
                self._kill_remote(self._require_remote_id(duplicate))
            else:
                logger.warning(
                    "multiple CubeSandboxes match user=%s thread=%s; keeping %s",
                    user,
                    thread,
                    self._require_remote_id(chosen) if chosen is not None else None,
                )

        if chosen is not None:
            remote_id = self._require_remote_id(chosen)
            metadata = self._row_metadata(chosen)
            if metadata.get("myflow.mounts_hash") == mount_hash:
                try:
                    return self._sdk_factory.connect(remote_id), False
                except Exception as exc:
                    raise SandboxRuntimeError(
                        f"CubeSandbox connect failed for {remote_id}: {exc}"
                    ) from exc

            # Under the per-thread lease we may replace a sandbox whose host
            # mounts no longer match the configured sources.
            logger.warning(
                "CubeSandbox %s has stale host mounts; recreating it", remote_id
            )
            self._kill_remote(remote_id)

        return self._create_sandbox(user, thread, mounts=mounts, mount_hash=mount_hash), True

    def _discover(self, user: str, thread: str | None) -> list[dict]:
        query = {
            "myflow": "1",
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
        }
        try:
            rows = self._sdk_factory.list_by_metadata(query)
        except Exception as exc:
            raise SandboxRuntimeError(
                f"CubeSandbox metadata discovery failed: {exc}"
            ) from exc
        if not isinstance(rows, list):
            raise SandboxRuntimeError("CubeSandbox metadata discovery returned non-list")

        matches: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            metadata = self._row_metadata(row)
            if str(metadata.get("myflow")) != "1":
                continue
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

    def _create_sandbox(
        self,
        user: str,
        thread: str | None,
        *,
        mounts: Sequence[MountEntry],
        mount_hash: str,
    ) -> SdkSandbox:
        metadata = self._metadata(user, thread, mounts=mounts, mount_hash=mount_hash)
        network: dict[str, Any] = {
            "allow_public_traffic": self._cube.allow_public_traffic,
            "allow_out": list(self._cube.network.allow_out),
            "deny_out": list(self._cube.network.deny_out),
        }
        if self._cube.network.rules is not None:
            network["rules"] = self._cube.network.rules
        try:
            return self._sdk_factory.create(
                template=self._cube.template_id,
                timeout=self._cube.timeout_seconds,
                metadata=metadata,
                lifecycle={
                    "on_timeout": self._cube.on_timeout,
                    "auto_resume": self._cube.auto_resume,
                },
                network=network,
                allow_internet_access=self._cube.allow_internet_access,
            )
        except Exception as exc:
            message = f"CubeSandbox create failed: {exc}"
            text = str(exc)
            if "hostPath" in text or "allowed mount prefix" in text:
                message += (
                    ". Check CubeMaster's allowed_host_mount_prefixes against "
                    "sandbox.cube.host_mount.host_root"
                )
            raise SandboxRuntimeError(message) from exc

    def _metadata(
        self,
        user: str,
        thread: str | None,
        *,
        mounts: Sequence[MountEntry],
        mount_hash: str,
    ) -> dict[str, str]:
        host_mount = json.dumps(
            [entry.as_payload() for entry in mounts],
            separators=(",", ":"),
        )
        return {
            "myflow": "1",
            "myflow.version": "1",
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
            "myflow.generic": "0" if thread else "1",
            "myflow.instance": self._instance_id,
            "myflow.mounts_hash": mount_hash,
            "host-mount": host_mount,
        }

    # -- canary -----------------------------------------------------------

    def _run_canary(self, sandbox: CubeSandbox, *, user: str, thread: str | None) -> None:
        """Prove host -> sandbox -> host reads and writes through the mount."""

        workspace = self._host_workspace(user, thread)
        canary_dir = workspace / _CANARY_DIR
        in_file = canary_dir / _CANARY_IN
        out_file = canary_dir / _CANARY_OUT
        try:
            canary_dir.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(16)
            in_file.write_text(token, encoding="utf-8")
            observed = sandbox.read_file(f"{_CANARY_CONTAINER}/{_CANARY_IN}")
            if observed.strip() != token:
                raise SandboxRuntimeError(
                    "the sandbox could not read the host-written canary token"
                )

            out_token = secrets.token_hex(16)
            sandbox.write_file(f"{_CANARY_CONTAINER}/{_CANARY_OUT}", out_token)
            if not out_file.is_file():
                raise SandboxRuntimeError(
                    "the sandbox write did not appear on the host mount"
                )
            host_token = out_file.read_text(encoding="utf-8")
            if host_token.strip() != out_token:
                raise SandboxRuntimeError(
                    "the host could not read the sandbox-written canary token"
                )
        finally:
            self._cleanup_canary(in_file, out_file, canary_dir)

    @staticmethod
    def _cleanup_canary(in_file: Path, out_file: Path, canary_dir: Path) -> None:
        for path in (in_file, out_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("could not remove canary probe %s", path, exc_info=True)
        try:
            canary_dir.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove canary directory %s", canary_dir, exc_info=True)

    # -- cache and leases -------------------------------------------------

    def _live_cached(self, key: tuple[str, str | None]) -> _CubeHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _take_live_cached(self, key: tuple[str, str | None]) -> _CubeHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                # Leave a stale handle in place for _drop_stale_locked, which
                # disposes of its lease outside the cache lock.
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _drop_stale_locked(self, key: tuple[str, str | None]) -> _CubeHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            if handle.alive and not handle.lease.lost:
                return None
            self._remove_locked(handle)
            return handle

    def _store_handle(self, handle: _CubeHandle) -> None:
        evicted: list[_CubeHandle] = []
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

    def _take_handle(self, sandbox_id: str) -> _CubeHandle | None:
        remote_id = self._remote_id_from_sandbox_id(sandbox_id)
        if remote_id is None:
            return None
        return self._take_handle_by_remote_id(remote_id)

    def _take_handle_by_remote_id(self, remote_id: str) -> _CubeHandle | None:
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

    def _remove_handle(self, handle: _CubeHandle) -> None:
        with self._cache_lock:
            self._remove_locked(handle)

    def _remove_locked(self, handle: _CubeHandle) -> None:
        handle.alive = False
        self._cache.pop(handle.key, None)
        self._by_remote_id.pop(handle.remote_id, None)

    def _drain_cache(self) -> list[_CubeHandle]:
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
            "CubeSandbox %s lost its Redis lease; dropping the local handle",
            handle.sandbox_id,
        )

    def _handle_reaped(self, remote_id: str) -> None:
        """Detach a handle whose remote sandbox answered 404/410.

        The MicroVM is gone, so neither pause nor kill would help: both are
        only a wasted control-plane call that fails the same way. Releasing
        the lease is what lets the next acquire re-lock the thread and build
        a fresh sandbox on the same host mounts.
        """

        handle = self._take_handle_by_remote_id(remote_id)
        if handle is None:
            return
        logger.warning(
            "CubeSandbox %s no longer exists; dropping the handle for re-acquire",
            handle.sandbox_id,
        )
        self._release_lease(handle)
        self._close_sdk(handle.sdk)

    @staticmethod
    def _release_lease(handle: _CubeHandle) -> None:
        CubeSandboxProvider._release_lease_from(handle.lease)

    @staticmethod
    def _release_lease_from(lease: LockLease) -> None:
        try:
            lease.release()
        except Exception:
            logger.warning("could not release CubeSandbox Redis lease", exc_info=True)

    def _key_lock_for(self, key: tuple[str, str | None]) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

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
            raise SandboxRuntimeError("CubeSandbox metadata row has no sandbox id")
        return remote_id

    def _host_workspace(self, user: str, thread: str | None) -> Path:
        if thread is None:
            return (
                self._paths.base_dir
                / "users"
                / user
                / "local"
                / "user-data"
                / "workspace"
            )
        return self._paths.sandbox_work_dir(thread, user_id=user)

    def _apply_action(self, sdk: SdkSandbox, action: str) -> None:
        try:
            if action == "kill":
                sdk.kill()
            elif action == "pause":
                sdk.pause()
            else:
                raise SandboxRuntimeError(f"unknown CubeSandbox release action: {action}")
        except Exception as exc:
            if is_gone_error(exc):
                return
            raise

    def _kill_remote(self, remote_id: str) -> None:
        try:
            sdk = self._sdk_factory.connect(remote_id)
        except Exception as exc:
            raise SandboxRuntimeError(
                f"CubeSandbox connect to stale instance {remote_id} failed: {exc}"
            ) from exc
        try:
            sdk.kill()
        except Exception as exc:
            if not is_gone_error(exc):
                raise SandboxRuntimeError(
                    f"CubeSandbox kill of stale instance {remote_id} failed: {exc}"
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
                logger.warning("could not kill a failed CubeSandbox", exc_info=True)

    @staticmethod
    def _close_sdk(sdk: SdkSandbox) -> None:
        try:
            sdk.close()
        except Exception:
            logger.warning("could not close a CubeSandbox SDK handle", exc_info=True)


__all__ = ["CubeSandboxProvider"]
