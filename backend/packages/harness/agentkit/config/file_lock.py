"""Best-effort directory fsync used by the atomic JSON writer."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


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


__all__ = ["fsync_directory_best_effort"]
