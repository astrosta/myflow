"""Process-level ContextForge tool snapshot and cache."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from agentkit.contextforge.catalog import ToolCatalogEntry


@dataclass(frozen=True, slots=True)
class ContextForgeSnapshot:
    tools: tuple[Any, ...]
    entries: tuple[ToolCatalogEntry, ...]
    loaded_at: float
    signature: str

    def is_stale(self, ttl_seconds: int) -> bool:
        return (time.time() - self.loaded_at) >= ttl_seconds


_lock = threading.Lock()
_snapshot: ContextForgeSnapshot | None = None


def get_contextforge_snapshot() -> ContextForgeSnapshot | None:
    with _lock:
        return _snapshot


def set_contextforge_snapshot(
    *,
    tools: tuple[Any, ...] | list[Any],
    entries: tuple[ToolCatalogEntry, ...] | list[ToolCatalogEntry],
) -> ContextForgeSnapshot:
    material = "\n".join(
        f"{entry.name}:{entry.content_hash}" for entry in sorted(entries, key=lambda item: item.name)
    )
    snapshot = ContextForgeSnapshot(
        tools=tuple(tools),
        entries=tuple(entries),
        loaded_at=time.time(),
        signature=hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
    )
    global _snapshot
    with _lock:
        _snapshot = snapshot
    return snapshot


def invalidate_contextforge_cache(reason: str = "") -> None:
    del reason
    global _snapshot
    with _lock:
        _snapshot = None


__all__ = [
    "ContextForgeSnapshot",
    "get_contextforge_snapshot",
    "invalidate_contextforge_cache",
    "set_contextforge_snapshot",
]
