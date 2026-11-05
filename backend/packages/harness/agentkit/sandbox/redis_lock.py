"""Redis-backed per-thread lock for sandbox creation and connection.

Gateway workers are separate processes, so an in-process lock cannot stop two
of them from creating or reconnecting the same thread sandbox at once. Every
acquire therefore takes this Redis lock first; the renewal lease keeps the TTL
alive only while the owner still holds the value.

Redis is imported lazily: a local-only deployment must not need the optional
extra just to import the sandbox package.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from agentkit.sandbox.exceptions import SandboxRuntimeError

if TYPE_CHECKING:
    import redis

logger = logging.getLogger(__name__)

# Short polls: the waiter must notice a release quickly without busy-looping.
_POLL_INTERVAL_SECONDS = 0.1
_MIN_TTL_MS = 1
_MIN_RENEW_INTERVAL_SECONDS = 0.001

# Compare-and-expire: a stale owner must never extend someone else's lock.
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

# Compare-and-delete: never delete another owner's value.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class LockNotAcquiredError(SandboxRuntimeError):
    """The per-thread lock stayed held by another owner past the timeout."""


class ThreadLock(Protocol):
    """Lock surface the provider depends on (Redis in production, fakes in tests)."""

    def ping(self) -> bool: ...

    def acquire(self, key: str, owner: str, timeout: float) -> None: ...

    def renew(self, key: str, owner: str) -> bool: ...

    def release(self, key: str, owner: str) -> None: ...


class RedisThreadLock(ThreadLock):
    """Exclusive, TTL-bounded, owner-checked lock in Redis.

    client is injectable so tests and the Gateway can share one connection
    pool. When it is omitted, the optional redis extra is imported here rather
    than at module scope.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        *,
        client: redis.Redis | None = None,
    ) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._client = client
        self._ttl_ms = max(_MIN_TTL_MS, int(ttl_seconds * 1000))

    def ping(self) -> bool:
        return bool(self._client.ping())

    def acquire(self, key: str, owner: str, timeout: float) -> None:
        """Take the lock or raise after timeout seconds of waiting."""

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._client.set(key, owner, nx=True, px=self._ttl_ms):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockNotAcquiredError(
                    f"lock {key} is held by another owner after {timeout:g}s"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def renew(self, key: str, owner: str) -> bool:
        """Extend the TTL only while owner still holds the value."""

        return bool(self._client.eval(_RENEW_SCRIPT, 1, key, owner, self._ttl_ms))

    def release(self, key: str, owner: str) -> None:
        """Delete the lock only when the value is still owner's."""

        self._client.eval(_RELEASE_SCRIPT, 1, key, owner)


class LockLease:
    """Hold one acquired lock open and report the moment ownership is lost."""

    def __init__(
        self,
        lock: ThreadLock,
        key: str,
        owner: str,
        ttl_seconds: float,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._lock = lock
        self._key = key
        self._owner = owner
        self._interval = max(ttl_seconds / 3.0, _MIN_RENEW_INTERVAL_SECONDS)
        self._on_lost = on_lost
        self._lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost

    def acquire(self, timeout: float) -> None:
        self._lock.acquire(self._key, self._owner, timeout)

    def start(self) -> None:
        """Start the daemon renewal thread (idempotent while it is alive)."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"myflow-lock-lease:{self._key}",
            daemon=True,
        )
        self._thread.start()

    def release(self) -> None:
        """Stop renewing, then delete the lock if this owner still holds it."""

        self._stop.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(self._interval * 2, _POLL_INTERVAL_SECONDS))
        self._thread = None
        self._lock.release(self._key, self._owner)

    def _renew_loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._lock.renew(self._key, self._owner)
            except Exception:
                # Fail closed: an unreachable Redis means ownership is unproven,
                # so the provider must stop treating this sandbox as serialized.
                logger.warning(
                    "lock %s renewal raised; treating the lease as lost",
                    self._key,
                    exc_info=True,
                )
                self._mark_lost()
                return
            if not renewed:
                self._mark_lost()
                return

    def _mark_lost(self) -> None:
        if self._lost:
            return
        self._lost = True
        if self._on_lost is not None:
            try:
                self._on_lost()
            except Exception:
                logger.exception("lock %s on_lost callback failed", self._key)


__all__ = [
    "LockLease",
    "LockNotAcquiredError",
    "RedisThreadLock",
    "ThreadLock",
]
