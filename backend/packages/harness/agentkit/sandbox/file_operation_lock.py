"""Per-(sandbox, path) write locks. Different sandboxes never share a lock."""

from __future__ import annotations

import threading
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentkit.sandbox.sandbox import Sandbox

_FILE_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)
_GUARD = threading.Lock()


def get_file_operation_lock(sandbox: Sandbox, path: str) -> threading.RLock:
    """Return the lock for one virtual path inside one sandbox.

    The key includes ``sandbox.id``. The same virtual path exists in every thread,
    and those sessions must not block each other.

    ``RLock`` so ``str_replace`` can hold the lock across ``write_file``.
    ``WeakValueDictionary`` so a long-lived process does not accumulate a lock per
    path forever; the lock disappears after the last holder releases it.
    """
    key = (sandbox.id or f"instance:{id(sandbox)}", path)
    with _GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock
