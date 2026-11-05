"""OpenSandboxProvider: one OpenSandbox sandbox per (user, thread).

The sandbox server owns containers, so this provider only decides *which*
sandbox a thread gets and keeps it exclusive:

- metadata carries the thread identity plus a mounts fingerprint, and discovery
  asks the server for exactly that metadata instead of scraping containers;
- a Redis per-thread lease serializes creation across Gateway workers, and an
  in-process per-key lock keeps one worker from racing itself;
- a cached handle is re-checked with get_info(), because a sandbox can reach its
  TTL or be killed by anything that talks to the same server. The sandbox is
  disposable; the host thread directory is the only source of truth.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentkit.config import AppConfig, get_app_config
from agentkit.config.paths import Paths, get_paths
from agentkit.config.sandbox_config import OpenSandboxConfig
from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from agentkit.sandbox.host_mounts import (
    MountEntry,
    build_host_mounts,
    mounts_hash,
    resolve_host_roots,
    validate_mount_sources,
)
from agentkit.sandbox.opensandbox.client import (
    OsbClient,
    OsbHandle,
    SandboxSummary,
    SdkOsbClient,
)
from agentkit.sandbox.opensandbox.sandbox import OpenSandboxSandbox
from agentkit.sandbox.redis_lock import LockLease, RedisThreadLock, ThreadLock
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.utils.ids import validate_thread_id, validate_user_id

logger = logging.getLogger(__name__)

_SANDBOX_PREFIX = "osb:"
_GENERIC_THREAD = "__generic__"
_USER_DATA_CONTAINER = "/mnt/user-data"
_CANARY_DIR = ".myflow-canary"
_CANARY_CONTAINER = _USER_DATA_CONTAINER + "/workspace/" + _CANARY_DIR
_FIELD_PREFIX = "sandbox.opensandbox.host_mount"

ClientFactory = Callable[[OpenSandboxConfig], OsbClient]


@dataclass
class _OsbHandle:
    """One cached sandbox plus the lease that makes it ours."""

    key: tuple[str, str | None]
    sandbox_id: str
    handle: OsbHandle
    sandbox: OpenSandboxSandbox
    lease: LockLease
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    alive: bool = True


class OpenSandboxProvider(SandboxProvider):
    """SandboxProvider backed by an OpenSandbox server."""

    uses_thread_data_mounts = True
    supports_agent_skill_isolation = True

    def __init__(
        self,
        *,
        app_config: AppConfig | None = None,
        client: OsbClient | None = None,
        client_factory: ClientFactory | None = None,
        lock: ThreadLock | None = None,
        paths: Paths | None = None,
        instance_id: str | None = None,
    ) -> None:
        config = app_config if app_config is not None else get_app_config()
        osb = config.sandbox.opensandbox
        if osb is None:
            raise SandboxRuntimeError(
                "sandbox.opensandbox is required for "
                "agentkit.sandbox.opensandbox:OpenSandboxProvider"
            )
        # Validate the configured root shape now. Mount sources are checked in
        # startup_check, because constructing the provider must not do IO.
        resolve_host_roots(osb.host_mount, field_prefix=_FIELD_PREFIX)

        self._app_config = config
        self._osb: OpenSandboxConfig = osb
        self._paths = paths if paths is not None else get_paths()
        self._instance_id = instance_id or uuid.uuid4().hex
        if client is not None:
            self._client: OsbClient = client
        elif client_factory is not None:
            self._client = client_factory(osb)
        else:
            self._client = _default_client(osb)
        if lock is None:
            try:
                lock = RedisThreadLock(osb.lease.redis_url, osb.lease.ttl_seconds)
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "OpenSandbox leases need the redis extra: install "
                    "agentkit[opensandbox,redis]"
                ) from exc
        self._lock: ThreadLock = lock
        self._max_cached_threads = max(1, int(osb.max_cached_threads))

        self._cache: OrderedDict[tuple[str, str | None], _OsbHandle] = OrderedDict()
        self._by_remote: dict[str, _OsbHandle] = {}
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
        """Fail startup on an unreachable server, a bad mount, or a bad Redis."""

        if self._osb.startup_healthcheck:
            try:
                self._client.ping()
            except Exception as exc:
                raise SandboxRuntimeError(
                    f"the OpenSandbox server at {self._osb.domain} is not reachable: "
                    f"{exc}. Start it with 'uvx opensandbox-server' or point "
                    "sandbox.opensandbox.domain at the running server"
                ) from exc
        validate_mount_sources(
            self._app_config,
            resolve_host_roots(self._osb.host_mount, field_prefix=_FIELD_PREFIX),
            field_prefix=_FIELD_PREFIX,
        )
        if self._lock.ping() is False:
            raise SandboxRuntimeError(
                "OpenSandbox lease Redis PING failed; "
                "sandbox.opensandbox.lease.redis_url must point at a reachable Redis"
            )
        self._handle_orphans()

    def desired_mounts_hash(self, user_id: str | None, thread_id: str | None) -> str:
        """Fingerprint the host mounts this provider would create right now."""

        user = validate_user_id(user_id or DEFAULT_USER_ID)
        thread = self._validate_thread(thread_id)
        return mounts_hash(self._build_mounts(user, thread))

    # -- lifecycle --------------------------------------------------------

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        user = validate_user_id(user_id or DEFAULT_USER_ID)
        thread = self._validate_thread(thread_id)
        key = (user, thread)

        cached = self._trusted_handle(key)
        if cached is not None:
            return cached.sandbox.id

        with self._key_lock_for(key):
            cached = self._take_live_cached(key)
            if cached is not None:
                return cached.sandbox.id
            return self._acquire_locked(key, user, thread)

    def get(self, sandbox_id: str) -> OpenSandboxSandbox | None:
        """Pure memory lookup. No server call."""

        key = self._key_from_sandbox_id(sandbox_id)
        if key is None:
            return None
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle.sandbox

    def release(self, sandbox_id: str) -> None:
        """Apply release_action to one owned sandbox, then release its lease."""

        handle = self._take_handle(sandbox_id)
        if handle is None:
            return
        try:
            if not handle.lease.lost and self._osb.release_action == "destroy":
                self._kill_quietly(handle.sandbox_id)
        finally:
            self._release_lease(handle)
            self._close_handle(handle.handle)

    def reset(self) -> None:
        """Drop local handles and leases. Remote sandboxes keep running."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            self._release_lease(handle)
            self._close_handle(handle.handle)

    def shutdown(self) -> None:
        """Kill every sandbox this provider still holds, then clear state."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            try:
                self._client.kill(handle.sandbox_id)
            except Exception:
                logger.warning(
                    "OpenSandbox %s kill failed during shutdown",
                    handle.sandbox_id,
                    exc_info=True,
                )
            finally:
                self._release_lease(handle)
                self._close_handle(handle.handle)
        self._close_client()

    # -- acquire internals ------------------------------------------------

    def _acquire_locked(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
    ) -> str:
        stale = self._drop_stale_locked(key)
        if stale is not None:
            # A handle whose lease was lost is not ours to destroy.
            self._release_lease(stale)
            self._close_handle(stale.handle)

        owner = self._instance_id + ":" + uuid.uuid4().hex
        lease = LockLease(
            self._lock,
            self._lock_key(user, thread),
            owner,
            self._osb.lease.ttl_seconds,
            on_lost=lambda: self._handle_lease_lost(key),
        )

        lease_acquired = False
        cached = False
        handle: _OsbHandle | None = None
        try:
            lease.acquire(self._osb.acquire_timeout_seconds)
            lease_acquired = True
            handle = self._discover_or_create(key, user, thread, lease)
            self._store_handle(handle)
            cached = True
            try:
                lease.start()
            except Exception:
                self._remove_handle(handle)
                cached = False
                raise
            return handle.sandbox.id
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxRuntimeError(f"OpenSandbox acquire failed: {exc}") from exc
        finally:
            if not cached:
                if handle is not None:
                    self._close_handle(handle.handle)
                if lease_acquired:
                    self._release_lease_from(lease)

    def _discover_or_create(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
        lease: LockLease,
    ) -> _OsbHandle:
        mounts = self._build_mounts(user, thread)
        fingerprint = mounts_hash(mounts)
        adopted = self._adopt_existing(
            user, thread, mounts=mounts, fingerprint=fingerprint, key=key, lease=lease
        )
        if adopted is not None:
            return adopted
        return self._create_sandbox(
            key, user, thread, mounts=mounts, fingerprint=fingerprint, lease=lease
        )

    def _adopt_existing(
        self,
        user: str,
        thread: str | None,
        *,
        mounts: Sequence[MountEntry],
        fingerprint: str,
        key: tuple[str, str | None],
        lease: LockLease,
    ) -> _OsbHandle | None:
        if not self._osb.discover_existing:
            return None
        rows = self._discover(user, thread)
        if not rows:
            return None
        ordered = sorted(rows, key=self._creation_order)
        chosen = ordered[0]
        for duplicate in ordered[1:]:
            if duplicate.metadata.get("myflow.instance") == self._instance_id:
                self._kill_quietly(duplicate.sandbox_id)
            else:
                logger.warning(
                    "multiple OpenSandbox sandboxes match %s; keeping %s",
                    key,
                    chosen.sandbox_id,
                )

        if str(chosen.metadata.get("myflow.mounts_hash") or "") != fingerprint:
            logger.warning(
                "OpenSandbox sandbox %s has stale host mounts; recreating it",
                chosen.sandbox_id,
            )
            self._kill_quietly(chosen.sandbox_id)
            return None

        try:
            handle = self._client.connect(chosen.sandbox_id)
        except Exception as exc:
            logger.warning(
                "OpenSandbox sandbox %s could not be reconnected (%s); recreating it",
                chosen.sandbox_id,
                exc,
            )
            self._kill_quietly(chosen.sandbox_id)
            return None
        return self._finish_handle(key, user, thread, handle, mounts=mounts, lease=lease)

    def _create_sandbox(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
        *,
        mounts: Sequence[MountEntry],
        fingerprint: str,
        lease: LockLease,
    ) -> _OsbHandle:
        metadata = self._metadata(key, fingerprint)
        try:
            handle = self._client.create(
                image=self._osb.image,
                timeout_seconds=self._osb.timeout_seconds,
                ready_timeout_seconds=self._osb.ready_timeout_seconds,
                env=dict(self._osb.env),
                metadata=metadata,
                mounts=mounts,
                resource=dict(self._osb.resource),
            )
        except Exception as exc:
            message = f"OpenSandbox create failed: {exc}"
            if "volume" in str(exc).lower() or "mount" in str(exc).lower():
                message += (
                    ". Check sandbox.opensandbox.host_mount: the sandbox server must "
                    "see the path that the Gateway local_root maps to"
                )
            raise SandboxRuntimeError(message) from exc
        return self._finish_handle(key, user, thread, handle, mounts=mounts, lease=lease)

    def _finish_handle(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
        handle: OsbHandle,
        *,
        mounts: Sequence[MountEntry],
        lease: LockLease,
    ) -> _OsbHandle:
        try:
            self._run_canary(handle, user, thread)
        except Exception:
            self._close_handle(handle)
            raise
        sandbox = OpenSandboxSandbox(
            handle,
            sandbox_id=self._sandbox_id(key),
            mounts=mounts,
            command_timeout=float(self._app_config.sandbox.bash_command_timeout),
            on_transport_failure=lambda: self._probe_sandbox(handle.sandbox_id),
        )
        return _OsbHandle(
            key=key,
            sandbox_id=handle.sandbox_id,
            handle=handle,
            sandbox=sandbox,
            lease=lease,
        )

    # -- mounts, metadata and identity ------------------------------------

    def _build_mounts(self, user: str, thread: str | None) -> tuple[MountEntry, ...]:
        return build_host_mounts(
            app_config=self._app_config,
            host_mount=self._osb.host_mount,
            user_id=user,
            thread_id=thread,
            paths=self._paths,
            field_prefix=_FIELD_PREFIX,
        )

    def _metadata(
        self, key: tuple[str, str | None], fingerprint: str
    ) -> dict[str, str]:
        return {
            "myflow": "1",
            "myflow.user": key[0],
            "myflow.thread": key[1] or _GENERIC_THREAD,
            "myflow.instance": self._instance_id,
            "myflow.mounts_hash": fingerprint,
            "myflow.created_at": f"{time.time():.0f}",
        }

    def _sandbox_id(self, key: tuple[str, str | None]) -> str:
        return _SANDBOX_PREFIX + key[0] + ":" + (key[1] or _GENERIC_THREAD)

    @staticmethod
    def _key_from_sandbox_id(sandbox_id: str) -> tuple[str, str | None] | None:
        if not isinstance(sandbox_id, str) or not sandbox_id.startswith(_SANDBOX_PREFIX):
            return None
        raw = sandbox_id[len(_SANDBOX_PREFIX):]
        user, sep, thread = raw.partition(":")
        if not sep or not user or not thread:
            return None
        return (user, None if thread == _GENERIC_THREAD else thread)

    # -- canary -----------------------------------------------------------

    def _run_canary(self, handle: OsbHandle, user: str, thread: str | None) -> None:
        """Prove host -> sandbox -> host reads and writes through the mount."""

        workspace = self._host_workspace(user, thread)
        canary_dir = workspace / _CANARY_DIR
        in_file = canary_dir / "in"
        out_file = canary_dir / "out"
        try:
            canary_dir.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(16)
            in_file.write_text(token, encoding="utf-8")
            observed = handle.read_text(_CANARY_CONTAINER + "/in")
            if observed.strip() != token:
                raise SandboxRuntimeError(
                    "the sandbox could not read the host-written canary token. Check "
                    "sandbox.opensandbox.host_mount and the server docker runtime"
                )

            out_token = secrets.token_hex(16)
            handle.write_text(_CANARY_CONTAINER + "/out", out_token)
            if not out_file.is_file():
                raise SandboxRuntimeError(
                    "the sandbox write did not appear on the host mount"
                )
            if out_file.read_text(encoding="utf-8").strip() != out_token:
                raise SandboxRuntimeError(
                    "the host could not read the sandbox-written canary token"
                )
        finally:
            _cleanup_canary(in_file, out_file, canary_dir)

    def _host_workspace(self, user: str, thread: str | None) -> Path:
        if thread is None:
            return (
                self._paths.base_dir / "users" / user / "local" / "user-data" / "workspace"
            )
        return self._paths.sandbox_work_dir(thread, user_id=user)

    # -- discovery, cache and leases --------------------------------------

    def _discover(self, user: str, thread: str | None) -> list[SandboxSummary]:
        query = {
            "myflow": "1",
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
        }
        try:
            rows = self._client.list_by_metadata(query)
        except Exception as exc:
            raise SandboxRuntimeError(
                f"OpenSandbox sandbox discovery failed: {exc}"
            ) from exc
        return [
            row
            for row in rows
            if row.metadata.get("myflow.user") == user
            and row.metadata.get("myflow.thread") == (thread or _GENERIC_THREAD)
        ]

    def _trusted_handle(self, key: tuple[str, str | None]) -> _OsbHandle | None:
        """Return a cached handle only after the server confirms the sandbox."""

        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
        if not self._sandbox_running(handle):
            self._handle_reaped(handle.sandbox_id)
            return None
        with self._cache_lock:
            if self._cache.get(key) is not handle:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _sandbox_running(self, handle: _OsbHandle) -> bool:
        try:
            return handle.handle.is_running()
        except SandboxNotFoundError:
            return False
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxRuntimeError(
                f"could not read the status of OpenSandbox {handle.sandbox_id}: {exc}"
            ) from exc

    def _take_live_cached(self, key: tuple[str, str | None]) -> _OsbHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _drop_stale_locked(self, key: tuple[str, str | None]) -> _OsbHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            if handle.alive and not handle.lease.lost:
                return None
            self._remove_locked(handle)
            return handle

    def _store_handle(self, handle: _OsbHandle) -> None:
        evicted: list[_OsbHandle] = []
        with self._cache_lock:
            while len(self._cache) >= self._max_cached_threads:
                _, old = self._cache.popitem(last=False)
                old.alive = False
                self._by_remote.pop(old.sandbox_id, None)
                evicted.append(old)
            self._cache[handle.key] = handle
            self._cache.move_to_end(handle.key)
            self._by_remote[handle.sandbox_id] = handle

        for old in evicted:
            # Eviction frees a process slot only: the sandbox keeps running and
            # its TTL (or release_action=destroy) decides its fate.
            self._release_lease(old)
            self._close_handle(old.handle)

    def _take_handle(self, sandbox_id: str) -> _OsbHandle | None:
        key = self._key_from_sandbox_id(sandbox_id)
        if key is None:
            return None
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            self._remove_locked(handle)
            return handle

    def _handle_reaped(self, remote_id: str) -> None:
        """Detach a handle whose sandbox is gone, releasing its lease."""

        with self._cache_lock:
            handle = self._by_remote.pop(remote_id, None)
            if handle is None:
                return
            handle.alive = False
            if self._cache.get(handle.key) is handle:
                self._cache.pop(handle.key, None)
        logger.warning(
            "OpenSandbox %s no longer exists; dropping the handle for re-acquire",
            remote_id,
        )
        self._release_lease(handle)
        self._close_handle(handle.handle)

    def _probe_sandbox(self, remote_id: str) -> None:
        """Transport failure handler: drop the handle only if the sandbox is gone."""

        try:
            handle = self._client.connect(remote_id)
        except SandboxNotFoundError:
            self._handle_reaped(remote_id)
            return
        except Exception:
            return
        try:
            running = handle.is_running()
        except Exception:
            return
        finally:
            self._close_handle(handle)
        if not running:
            self._handle_reaped(remote_id)

    def _remove_handle(self, handle: _OsbHandle) -> None:
        with self._cache_lock:
            self._remove_locked(handle)

    def _remove_locked(self, handle: _OsbHandle) -> None:
        handle.alive = False
        if self._cache.get(handle.key) is handle:
            self._cache.pop(handle.key, None)
        self._by_remote.pop(handle.sandbox_id, None)

    def _drain_cache(self) -> list[_OsbHandle]:
        with self._cache_lock:
            handles = list(self._cache.values())
            self._cache.clear()
            self._by_remote.clear()
        return handles

    def _handle_lease_lost(self, key: tuple[str, str | None]) -> None:
        """Detach a lease-lost handle without touching the remote sandbox."""

        with self._cache_lock:
            handle = self._cache.pop(key, None)
            if handle is None:
                return
            handle.alive = False
            self._by_remote.pop(handle.sandbox_id, None)
        logger.warning(
            "OpenSandbox %s lost its Redis lease; dropping the local handle",
            handle.sandbox_id,
        )

    def _handle_orphans(self) -> None:
        """Report or remove sandboxes nobody in this process owns."""

        try:
            rows = self._client.list_by_metadata(
                {"myflow": "1"}, states=("RUNNING", "PAUSED")
            )
        except Exception:
            logger.warning("OpenSandbox orphan scan failed", exc_info=True)
            return
        foreign = [
            row
            for row in rows
            if row.metadata.get("myflow.instance") != self._instance_id
        ]
        if not foreign:
            return
        if self._osb.orphan_policy == "remove":
            deadline = time.time() - self._osb.orphan_ttl_seconds
            for row in foreign:
                created = _created_at(row.metadata)
                if created is not None and created > deadline:
                    continue
                self._kill_quietly(row.sandbox_id)
            return
        ids = ", ".join(row.sandbox_id for row in foreign)
        logger.warning(
            "OpenSandbox sandboxes from another gateway instance are still present: "
            "%s. They are not touched automatically; set "
            "sandbox.opensandbox.orphan_policy=remove once no gateway needs them",
            ids,
        )

    def _kill_quietly(self, remote_id: str) -> None:
        try:
            self._client.kill(remote_id)
        except Exception:
            logger.warning("could not kill OpenSandbox %s", remote_id, exc_info=True)

    @staticmethod
    def _release_lease(handle: _OsbHandle) -> None:
        OpenSandboxProvider._release_lease_from(handle.lease)

    @staticmethod
    def _release_lease_from(lease: LockLease) -> None:
        try:
            lease.release()
        except Exception:
            logger.warning("could not release the OpenSandbox Redis lease", exc_info=True)

    @staticmethod
    def _close_handle(handle: OsbHandle) -> None:
        try:
            handle.close()
        except Exception:
            logger.warning("could not close an OpenSandbox handle", exc_info=True)

    def _close_client(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            logger.warning("could not close the OpenSandbox client", exc_info=True)

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
        return "myflow:sandbox:" + user + ":" + (thread or _GENERIC_THREAD)

    @staticmethod
    def _creation_order(row: SandboxSummary) -> tuple[int, str]:
        created = _created_at(row.metadata)
        if created is None:
            return (1, "")
        return (0, f"{created:020.6f}")


def _created_at(metadata: Mapping[str, str]) -> float | None:
    raw = metadata.get("myflow.created_at")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _default_client(config: OpenSandboxConfig) -> OsbClient:
    return SdkOsbClient(
        config.domain,
        protocol=config.protocol,
        api_key=config.api_key,
        request_timeout=config.request_timeout_seconds,
    )


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


__all__ = ["OpenSandboxProvider"]
