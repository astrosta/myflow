"""AioSandboxProviderDocker: one Docker container per (user, thread).

The container is the sandbox and the host thread directory is the workspace:
these are the same bind mounts CubeSandbox uses, so switching providers moves no
data. A Redis per-thread lease serializes creation across Gateway workers (they
share one Docker daemon), and an in-process per-key lock keeps one worker from
racing itself.

A cached handle is never trusted blindly: acquire re-checks the container with
docker inspect, because anything on the host can remove it. The container is
disposable; the host directory is the only source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentkit.config import AppConfig, get_app_config
from agentkit.config.paths import Paths, get_paths
from agentkit.config.sandbox_config import AioSandboxConfig
from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.sandbox.aio_sandbox.client import AioClient, SdkAioClient
from agentkit.sandbox.aio_sandbox.docker import (
    ContainerSpec,
    ContainerState,
    DockerClient,
    DockerContainerError,
    Topology,
    resolve_topology,
)
from agentkit.sandbox.aio_sandbox.sandbox import AioSandbox
from agentkit.sandbox.exceptions import SandboxError, SandboxRuntimeError
from agentkit.sandbox.host_mounts import (
    MountEntry,
    build_host_mounts,
    mounts_hash,
    resolve_host_roots,
    validate_mount_sources,
)
from agentkit.sandbox.redis_lock import LockLease, RedisThreadLock, ThreadLock
from agentkit.sandbox.sandbox_provider import SandboxProvider
from agentkit.utils.ids import validate_thread_id, validate_user_id

logger = logging.getLogger(__name__)

_SANDBOX_PREFIX = "aio:"
_GENERIC_THREAD = "__generic__"
_USER_DATA_CONTAINER = "/mnt/user-data"
_CANARY_DIR = ".myflow-canary"
_CANARY_CONTAINER = _USER_DATA_CONTAINER + "/workspace/" + _CANARY_DIR
_CANARY_IN = "in"
_CANARY_OUT = "out"
_FIELD_PREFIX = "sandbox.aio.host_mount"
_READY_POLL_SECONDS = 0.2

ClientFactory = Callable[[str], AioClient]


@dataclass
class _AioHandle:
    """One cached container plus the lease that makes it ours."""

    key: tuple[str, str | None]
    container_name: str
    container_id: str
    base_url: str
    client: AioClient
    sandbox: AioSandbox
    lease: LockLease
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    alive: bool = True


class AioSandboxProviderDocker(SandboxProvider):
    """SandboxProvider backed by all-in-one-sandbox containers."""

    uses_thread_data_mounts = True
    supports_agent_skill_isolation = True

    def __init__(
        self,
        *,
        app_config: AppConfig | None = None,
        docker_client: DockerClient | None = None,
        client_factory: ClientFactory | None = None,
        lock: ThreadLock | None = None,
        paths: Paths | None = None,
        instance_id: str | None = None,
    ) -> None:
        config = app_config if app_config is not None else get_app_config()
        aio = config.sandbox.aio
        if aio is None:
            raise SandboxRuntimeError(
                "sandbox.aio is required for "
                "agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker"
            )
        # Validate the configured root shape now. Mount sources are checked in
        # startup_check, because constructing the provider must not do IO.
        resolve_host_roots(aio.host_mount, field_prefix=_FIELD_PREFIX)

        self._app_config = config
        self._aio: AioSandboxConfig = aio
        self._paths = paths if paths is not None else get_paths()
        self._instance_id = instance_id or uuid.uuid4().hex
        self._docker = docker_client or DockerClient()
        self._client_factory: ClientFactory = client_factory or self._default_client
        if lock is None:
            try:
                lock = RedisThreadLock(aio.lease.redis_url, aio.lease.ttl_seconds)
            except ImportError as exc:
                raise SandboxRuntimeError(
                    "AIO sandbox leases need the redis extra: install agentkit[aio,redis]"
                ) from exc
        self._lock: ThreadLock = lock
        self._max_cached_threads = max(1, int(aio.max_cached_threads))
        self._topology: Topology | None = None

        self._cache: OrderedDict[tuple[str, str | None], _AioHandle] = OrderedDict()
        self._by_container: dict[str, _AioHandle] = {}
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
        """Fail startup on an unreachable daemon, a bad topology, or a bad mount."""

        if self._aio.startup_healthcheck:
            try:
                version = self._docker.version()
            except Exception as exc:
                raise SandboxRuntimeError(
                    f"the docker daemon is not reachable: {exc}. The AIO provider "
                    "needs a working docker CLI and daemon"
                ) from exc
            logger.info("AIO sandbox provider: docker %s", version or "unknown")
            if not self._docker.image_exists(self._aio.image):
                logger.warning(
                    "AIO sandbox image %s is not present locally; it will be pulled "
                    "on the first acquire",
                    self._aio.image,
                )
        self._resolve_topology()
        validate_mount_sources(
            self._app_config,
            resolve_host_roots(self._aio.host_mount, field_prefix=_FIELD_PREFIX),
            field_prefix=_FIELD_PREFIX,
        )
        if self._lock.ping() is False:
            raise SandboxRuntimeError(
                "AIO sandbox lease Redis PING failed; sandbox.aio.lease.redis_url "
                "must point at a reachable Redis"
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
            # Another caller in this process may have populated the cache while
            # we waited for the per-key lock.
            cached = self._take_live_cached(key)
            if cached is not None:
                return cached.sandbox.id
            return self._acquire_locked(key, user, thread)

    def get(self, sandbox_id: str) -> AioSandbox | None:
        """Pure memory lookup. No docker call, no HTTP call."""

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
        """Apply release_action to one owned container, then release its lease."""

        handle = self._take_handle(sandbox_id)
        if handle is None:
            return
        try:
            if not handle.lease.lost:
                self._apply_release_action(handle.container_name)
        except Exception:
            logger.warning(
                "AIO sandbox %s %s failed; releasing the lease anyway",
                handle.container_name,
                self._aio.release_action,
                exc_info=True,
            )
        finally:
            self._release_lease(handle)
            self._close_client(handle.client)

    def reset(self) -> None:
        """Drop local handles and leases. Containers keep running."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            self._release_lease(handle)
            self._close_client(handle.client)

    def shutdown(self) -> None:
        """Remove every container this provider still holds, then clear state."""

        handles = self._drain_cache()
        for handle in handles:
            handle.alive = False
            try:
                self._docker.remove(handle.container_name)
            except Exception:
                logger.warning(
                    "AIO sandbox %s removal failed during shutdown",
                    handle.container_name,
                    exc_info=True,
                )
            finally:
                self._release_lease(handle)
                self._close_client(handle.client)

    # -- acquire internals ------------------------------------------------

    def _acquire_locked(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
    ) -> str:
        stale = self._drop_stale_locked(key)
        if stale is not None:
            # A handle whose lease was lost is not ours to stop or remove.
            self._release_lease(stale)
            self._close_client(stale.client)

        owner = self._instance_id + ":" + uuid.uuid4().hex
        lease = LockLease(
            self._lock,
            self._lock_key(user, thread),
            owner,
            self._aio.lease.ttl_seconds,
            on_lost=lambda: self._handle_lease_lost(key),
        )

        lease_acquired = False
        cached = False
        handle: _AioHandle | None = None
        try:
            lease.acquire(self._aio.acquire_timeout_seconds)
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
            raise SandboxRuntimeError(f"AIO sandbox acquire failed: {exc}") from exc
        finally:
            if not cached:
                if handle is not None:
                    self._close_client(handle.client)
                if lease_acquired:
                    self._release_lease_from(lease)

    def _discover_or_create(
        self,
        key: tuple[str, str | None],
        user: str,
        thread: str | None,
        lease: LockLease,
    ) -> _AioHandle:
        mounts = self._build_mounts(user, thread)
        fingerprint = mounts_hash(mounts)
        rows = self._discover(user, thread)
        adopted = self._adopt_existing(
            rows, mounts=mounts, fingerprint=fingerprint, key=key, lease=lease
        )
        if adopted is not None:
            return adopted
        return self._create_container(
            key, mounts=mounts, fingerprint=fingerprint, lease=lease
        )

    def _adopt_existing(
        self,
        rows: Sequence[ContainerState],
        *,
        mounts: Sequence[MountEntry],
        fingerprint: str,
        key: tuple[str, str | None],
        lease: LockLease,
    ) -> _AioHandle | None:
        if not rows:
            return None
        ordered = sorted(rows, key=self._creation_order)
        chosen = ordered[0]
        for duplicate in ordered[1:]:
            if duplicate.labels.get("myflow.instance") == self._instance_id:
                self._remove_container_quietly(duplicate.name or duplicate.container_id)
            else:
                logger.warning(
                    "multiple AIO sandbox containers match %s; keeping %s",
                    key,
                    chosen.name or chosen.container_id,
                )

        stored = str(chosen.labels.get("myflow.mounts_hash") or "")
        if stored != fingerprint:
            logger.warning(
                "AIO sandbox %s has stale host mounts; recreating it",
                chosen.name or chosen.container_id,
            )
            self._remove_container_quietly(chosen.name or chosen.container_id)
            return None

        name = chosen.name or chosen.container_id
        if not chosen.running:
            self._docker.start(name)
        return self._connect_existing(key, name, lease, mounts=mounts)

    def _connect_existing(
        self,
        key: tuple[str, str | None],
        name: str,
        lease: LockLease,
        *,
        mounts: Sequence[MountEntry],
    ) -> _AioHandle:
        state = self._docker.inspect(name, container_port=self._aio.port)
        if state.host_port is None:
            self._remove_container_quietly(name)
            raise SandboxRuntimeError(
                f"AIO sandbox container {name} has no published port. Remove it "
                "with: docker rm -f " + name
            )
        base_url = self._base_url(state.host_port)
        client = self._client_factory(base_url)
        try:
            self._wait_until_ready(client, name)
            self._run_canary(client, key[0], key[1])
        except Exception:
            self._close_client(client)
            raise

        sandbox = AioSandbox(
            client,
            sandbox_id=self._sandbox_id(key),
            mounts=mounts,
            command_timeout=float(self._app_config.sandbox.bash_command_timeout),
            on_transport_failure=lambda: self._probe_container(name),
        )
        return _AioHandle(
            key=key,
            container_name=name,
            container_id=state.container_id,
            base_url=base_url,
            client=client,
            sandbox=sandbox,
            lease=lease,
        )

    def _create_container(
        self,
        key: tuple[str, str | None],
        *,
        mounts: Sequence[MountEntry],
        fingerprint: str,
        lease: LockLease,
    ) -> _AioHandle:
        name = self._container_name(*key)
        topology = self._resolve_topology()
        labels = {
            "myflow": "1",
            "myflow.provider": "aio",
            "myflow.user": key[0],
            "myflow.thread": key[1] or _GENERIC_THREAD,
            "myflow.instance": self._instance_id,
            "myflow.mounts_hash": fingerprint,
            "myflow.created_at": f"{time.time():.0f}",
        }
        resources = self._aio.resources
        spec = ContainerSpec(
            name=name,
            image=self._aio.image,
            labels=labels,
            mounts=mounts,
            publish_host=topology.publish_host,
            container_port=self._aio.port,
            env=dict(self._aio.env),
            network_mode=self._aio.network.mode,
            network_name=self._aio.network.name,
            cpus=resources.cpus,
            memory=resources.memory,
            pids_limit=resources.pids_limit,
        )
        try:
            self._docker.run(spec)
        except DockerContainerError as exc:
            if exc.name_in_use:
                # Another worker won the race under the same lease owner name.
                return self._connect_existing(key, name, lease, mounts=mounts)
            message = f"AIO sandbox create failed: {exc}"
            if "mount" in str(exc).lower() or "hostPath" in str(exc):
                message += (
                    ". Check sandbox.aio.host_mount: the Docker daemon must see the "
                    "path that the Gateway's local_root maps to"
                )
            raise SandboxRuntimeError(message) from exc

        try:
            return self._connect_existing(key, name, lease, mounts=mounts)
        except Exception:
            self._remove_container_quietly(name)
            raise

    # -- mounts and identity ----------------------------------------------

    def _build_mounts(self, user: str, thread: str | None) -> tuple[MountEntry, ...]:
        return build_host_mounts(
            app_config=self._app_config,
            host_mount=self._aio.host_mount,
            user_id=user,
            thread_id=thread,
            paths=self._paths,
            field_prefix=_FIELD_PREFIX,
        )

    def _base_url(self, host_port: int) -> str:
        return "http://" + self._resolve_topology().connect_host + ":" + str(host_port)

    def _resolve_topology(self) -> Topology:
        if self._topology is None:
            self._topology = resolve_topology(
                self._aio, gateway_lookup=self._docker.bridge_gateway
            )
        return self._topology

    def _container_name(self, user: str, thread: str | None) -> str:
        seed = (user + "\0" + (thread or _GENERIC_THREAD)).encode("utf-8")
        digest = hashlib.sha256(seed).hexdigest()[:12]
        return self._aio.container_name_prefix + "-" + digest

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

    # -- readiness and canary ---------------------------------------------

    def _wait_until_ready(self, client: AioClient, name: str) -> None:
        deadline = time.monotonic() + self._aio.ready_timeout_seconds
        last_error: Exception | None = None
        while True:
            try:
                client.ping()
                return
            except Exception as exc:  # noqa: BLE001 - retried until the deadline
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxRuntimeError(
                    "AIO sandbox container "
                    + name
                    + " did not become ready within "
                    + f"{self._aio.ready_timeout_seconds:g}s: {last_error}. The image "
                    "must run its sandbox server on sandbox.aio.port"
                )
            time.sleep(min(_READY_POLL_SECONDS, remaining))

    def _run_canary(self, client: AioClient, user: str, thread: str | None) -> None:
        """Prove host -> container -> host reads and writes through the mount."""

        workspace = self._host_workspace(user, thread)
        canary_dir = workspace / _CANARY_DIR
        in_file = canary_dir / _CANARY_IN
        out_file = canary_dir / _CANARY_OUT
        try:
            canary_dir.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(16)
            in_file.write_text(token, encoding="utf-8")
            observed = client.read_text(_CANARY_CONTAINER + "/" + _CANARY_IN)
            if observed.strip() != token:
                raise SandboxRuntimeError(
                    "the sandbox could not read the host-written canary token"
                )

            out_token = secrets.token_hex(16)
            client.write_text(_CANARY_CONTAINER + "/" + _CANARY_OUT, out_token)
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
            _cleanup_canary(in_file, out_file, canary_dir)

    def _host_workspace(self, user: str, thread: str | None) -> Path:
        if thread is None:
            return self._paths.base_dir / "users" / user / "local" / "user-data" / "workspace"
        return self._paths.sandbox_work_dir(thread, user_id=user)

    # -- discovery, cache and leases --------------------------------------

    def _discover(self, user: str, thread: str | None) -> list[ContainerState]:
        labels = {
            "myflow": "1",
            "myflow.provider": "aio",
            "myflow.user": user,
            "myflow.thread": thread or _GENERIC_THREAD,
        }
        try:
            rows = self._docker.list_by_labels(labels)
        except Exception as exc:
            raise SandboxRuntimeError(
                f"AIO sandbox container discovery failed: {exc}"
            ) from exc
        matches = [
            row
            for row in rows
            if row.labels.get("myflow.user") == user
            and row.labels.get("myflow.thread") == (thread or _GENERIC_THREAD)
        ]
        return matches

    def _trusted_handle(self, key: tuple[str, str | None]) -> _AioHandle | None:
        """Return a cached handle only after docker confirms the container."""

        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                return None
        if not self._container_usable(handle):
            self._handle_reaped(handle.container_name)
            return None
        with self._cache_lock:
            if self._cache.get(key) is not handle:
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _container_usable(self, handle: _AioHandle) -> bool:
        try:
            state = self._docker.inspect(
                handle.container_name, container_port=self._aio.port
            )
        except DockerContainerError as exc:
            if exc.not_found:
                return False
            raise SandboxRuntimeError(
                f"could not inspect AIO sandbox {handle.container_name}: {exc}"
            ) from exc
        if state.running:
            return True
        try:
            self._docker.start(handle.container_name)
            self._wait_until_ready(handle.client, handle.container_name)
        except Exception:
            logger.warning(
                "AIO sandbox %s could not be restarted; recreating it",
                handle.container_name,
                exc_info=True,
            )
            return False
        return True

    def _take_live_cached(self, key: tuple[str, str | None]) -> _AioHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None or not handle.alive or handle.lease.lost:
                # Leave a stale handle in place for _drop_stale_locked, which
                # disposes of its lease outside the cache lock.
                return None
            handle.last_used_at = time.monotonic()
            self._cache.move_to_end(key)
            return handle

    def _drop_stale_locked(self, key: tuple[str, str | None]) -> _AioHandle | None:
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            if handle.alive and not handle.lease.lost:
                return None
            self._remove_locked(handle)
            return handle

    def _store_handle(self, handle: _AioHandle) -> None:
        evicted: list[_AioHandle] = []
        with self._cache_lock:
            while len(self._cache) >= self._max_cached_threads:
                _, old = self._cache.popitem(last=False)
                old.alive = False
                self._by_container.pop(old.container_name, None)
                evicted.append(old)
            self._cache[handle.key] = handle
            self._cache.move_to_end(handle.key)
            self._by_container[handle.container_name] = handle

        for old in evicted:
            # Eviction frees a process slot only. Stop the container so it does
            # not hold resources, but never remove it: another worker may own it.
            self._apply_release_action_quietly(old.container_name)
            self._release_lease(old)
            self._close_client(old.client)

    def _take_handle(self, sandbox_id: str) -> _AioHandle | None:
        key = self._key_from_sandbox_id(sandbox_id)
        if key is None:
            return None
        with self._cache_lock:
            handle = self._cache.get(key)
            if handle is None:
                return None
            self._remove_locked(handle)
            return handle

    def _handle_reaped(self, container_name: str) -> None:
        """Detach a handle whose container is gone, releasing its lease."""

        with self._cache_lock:
            handle = self._by_container.pop(container_name, None)
            if handle is None:
                return
            handle.alive = False
            if self._cache.get(handle.key) is handle:
                self._cache.pop(handle.key, None)
        logger.warning(
            "AIO sandbox %s no longer exists; dropping the handle for re-acquire",
            container_name,
        )
        self._release_lease(handle)
        self._close_client(handle.client)

    def _probe_container(self, container_name: str) -> None:
        """Transport failure handler: drop the handle only if the container is gone."""

        try:
            state = self._docker.inspect(container_name, container_port=self._aio.port)
        except DockerContainerError as exc:
            if exc.not_found:
                self._handle_reaped(container_name)
            return
        if not state.running:
            self._handle_reaped(container_name)

    def _remove_handle(self, handle: _AioHandle) -> None:
        with self._cache_lock:
            self._remove_locked(handle)

    def _remove_locked(self, handle: _AioHandle) -> None:
        handle.alive = False
        if self._cache.get(handle.key) is handle:
            self._cache.pop(handle.key, None)
        self._by_container.pop(handle.container_name, None)

    def _drain_cache(self) -> list[_AioHandle]:
        with self._cache_lock:
            handles = list(self._cache.values())
            self._cache.clear()
            self._by_container.clear()
        return handles

    def _handle_lease_lost(self, key: tuple[str, str | None]) -> None:
        """Detach a lease-lost handle without touching the container.

        Another worker may already own the thread; the old handle must stop
        serving new calls so the runtime re-acquires and re-locks.
        """

        with self._cache_lock:
            handle = self._cache.pop(key, None)
            if handle is None:
                return
            handle.alive = False
            self._by_container.pop(handle.container_name, None)
        logger.warning(
            "AIO sandbox %s lost its Redis lease; dropping the local handle",
            handle.container_name,
        )

    def _handle_orphans(self) -> None:
        """Report or remove containers nobody in this process owns."""

        try:
            rows = self._docker.list_by_labels({"myflow": "1", "myflow.provider": "aio"})
        except Exception:
            logger.warning("AIO sandbox orphan scan failed", exc_info=True)
            return
        foreign = [
            row
            for row in rows
            if row.labels.get("myflow.instance") != self._instance_id
        ]
        if not foreign:
            return
        if self._aio.orphan_policy == "remove":
            deadline = time.time() - self._aio.orphan_ttl_seconds
            for row in foreign:
                created = _created_at(row)
                if created is not None and created > deadline:
                    continue
                self._remove_container_quietly(row.name or row.container_id)
            return
        names = ", ".join(row.name or row.container_id for row in foreign)
        logger.warning(
            "AIO sandbox containers from another gateway instance are still present: "
            "%s. They are not touched automatically; remove them with "
            "docker rm -f <name> once no gateway needs them",
            names,
        )

    def _apply_release_action(self, container_name: str) -> None:
        if self._aio.release_action == "remove":
            self._docker.remove(container_name)
        else:
            self._docker.stop(container_name)

    def _apply_release_action_quietly(self, container_name: str) -> None:
        try:
            self._apply_release_action(container_name)
        except Exception:
            logger.warning(
                "AIO sandbox %s %s failed during eviction",
                container_name,
                self._aio.release_action,
                exc_info=True,
            )

    def _remove_container_quietly(self, container_name: str) -> None:
        try:
            self._docker.remove(container_name)
        except Exception:
            logger.warning(
                "could not remove AIO sandbox container %s", container_name, exc_info=True
            )

    @staticmethod
    def _release_lease(handle: _AioHandle) -> None:
        AioSandboxProviderDocker._release_lease_from(handle.lease)

    @staticmethod
    def _release_lease_from(lease: LockLease) -> None:
        try:
            lease.release()
        except Exception:
            logger.warning("could not release the AIO sandbox Redis lease", exc_info=True)

    @staticmethod
    def _close_client(client: AioClient) -> None:
        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            logger.warning("could not close an AIO sandbox client", exc_info=True)

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
    def _creation_order(row: ContainerState) -> tuple[int, str]:
        created = _created_at(row)
        if created is None:
            return (1, "")
        return (0, f"{created:020.6f}")

    def _default_client(self, base_url: str) -> AioClient:
        return SdkAioClient(base_url, timeout=self._aio.request_timeout_seconds)


def _created_at(row: ContainerState) -> float | None:
    raw = row.labels.get("myflow.created_at")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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


__all__ = ["AioSandboxProviderDocker"]
