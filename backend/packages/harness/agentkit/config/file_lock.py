"""跨进程文件咨询锁（M7 §2.2）。Windows 用 msvcrt，POSIX 用 fcntl。

M7 有两份"既被 API 写、又被运行时读"的 JSON：extensions_config.json 和
skills_prefs.json。两份文件各用**自己**的锁（互不阻塞），但锁的实现只有这一份。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.05

try:  # POSIX 咨询锁
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows 咨询锁
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


@contextmanager
def advisory_file_lock(path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """锁的是 <path> 旁边的 .<name>.lock。

    锁文件建好就不删——删掉之后两个进程可以在"open 旧的"和"创建新的"之间
    各锁一个 inode，锁形同虚设。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    with open(lock_path, "a+b") as handle:
        _acquire(handle, timeout=timeout)
        try:
            yield
        finally:
            _release(handle)


def _acquire(handle, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            _try_lock(handle)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the file lock") from None
            time.sleep(_LOCK_RETRY_SECONDS)


def _try_lock(handle) -> None:
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    logger.warning("no advisory file lock available on this platform")  # pragma: no cover


def _release(handle) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - 释放失败不掩盖函数体真正的异常
        logger.debug("failed to release the file lock", exc_info=True)


def fsync_directory_best_effort(directory: Path) -> None:
    if os.name == "nt":  # Windows 打不开目录句柄
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        handle = os.open(directory, flags)
    except OSError:
        logger.debug("cannot open %s for fsync", directory, exc_info=True)
        return
    try:
        os.fsync(handle)
    except OSError:
        logger.debug("directory fsync failed for %s", directory, exc_info=True)
    finally:
        os.close(handle)
