"""CubeSandbox per-thread Redis lock (Task 4)."""

from __future__ import annotations

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


def test_ping_delegates_to_client() -> None:
    assert _lock().ping() is True


def test_lease_release_stops_renewal_and_releases() -> None:
    class CountingLock:
        def __init__(self) -> None:
            self.renewed = 0
            self.released: list[tuple[str, str]] = []

        def ping(self) -> bool:
            return True

        def acquire(self, key, owner, timeout): return None

        def renew(self, key, owner):
            self.renewed += 1
            return True

        def release(self, key, owner):
            self.released.append((key, owner))

    lock = CountingLock()
    lease = LockLease(lock, "k", "owner", 0.01)
    lease.acquire(timeout=0.1)
    lease.start()
    deadline = time.monotonic() + 1
    while not lock.renewed and time.monotonic() < deadline:
        time.sleep(0.01)

    lease.release()

    assert lease.lost is False
    assert lock.released == [("k", "owner")]
    renews_at_release = lock.renewed
    time.sleep(0.05)
    assert lock.renewed == renews_at_release
