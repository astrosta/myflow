"""Explicit host <-> sandbox file sync for the E2B provider.

The E2B protocol has no host mounts.  Acquire uploads the thread's host
uploads/ tree, release downloads outputs/, and skill files are uploaded only
when a tool first reads them.  Every transfer is bounded by
sandbox.e2b.sync.max_file_bytes / max_tree_bytes: an oversized file is skipped
with a warning, an oversized tree is abandoned with a warning.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shlex
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentkit.sandbox.e2b.client import SdkSandbox, is_missing_path_error

logger = logging.getLogger(__name__)

_MKDIR_TIMEOUT_SECONDS = 60.0
_MKDIR_BATCH = 100
_SAFE_NAME = re.compile(r"^[^/\\\x00]+$")


@dataclass(frozen=True, slots=True)
class SyncLimits:
    """Byte caps shared by uploads, downloads, and lazy skill uploads."""

    max_file_bytes: int = 32 * 1024 * 1024
    max_tree_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SyncStats:
    """What one tree transfer did; the caller logs the summary."""

    files: int = 0
    bytes: int = 0
    skipped_files: int = 0
    skipped_trees: int = 0


@dataclass(frozen=True, slots=True)
class SkillSource:
    """One read-only host tree projected into the sandbox.

    The projection is virtual: the file is uploaded on first access, not bind
    mounted.  container_root is the longest-prefix key for a tool path.
    """

    host_root: Path
    container_root: str


class SkillUploadTooLarge(OSError):
    """A lazy skill upload cannot fit inside the configured sync caps."""


@dataclass(frozen=True, slots=True)
class _TreePlan:
    dirs: tuple[Path, ...]
    files: tuple[tuple[Path, int], ...]
    total_bytes: int


def ensure_container_dirs(
    sdk: SdkSandbox, paths: Iterable[str], *, user: str
) -> None:
    """Create fixed sandbox directories with mkdir -p.

    This is the one place sync uses the shell instead of the file API: the E2B
    file API has no recursive create, and the template is already required to
    ship mkdir.
    """

    unique = []
    seen: set[str] = set()
    for path in paths:
        normalized = posixpath.normpath(path)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    for start in range(0, len(unique), _MKDIR_BATCH):
        chunk = unique[start : start + _MKDIR_BATCH]
        command = "mkdir -p -- " + " ".join(shlex.quote(item) for item in chunk)
        result = sdk.commands.run(command, timeout=_MKDIR_TIMEOUT_SECONDS, user=user)
        code = getattr(result, "exit_code", 0)
        if code not in (0, None):
            raise OSError(f"Failed to create sandbox directories: exit code {code}")


def upload_tree(
    sdk: SdkSandbox,
    *,
    host_root: Path,
    container_root: str,
    limits: SyncLimits,
    user: str,
    label: str,
) -> SyncStats:
    """Upload one host tree without truncating anything.

    A missing host root is an empty tree.  A tree over max_tree_bytes is
    abandoned before the first write; a file over max_file_bytes is skipped.
    Read/transport failures raise: acquire treats them as fatal.
    """

    host_root = Path(host_root)
    plan = _scan_tree(host_root)
    if plan is None:
        return SyncStats()
    if plan.total_bytes > limits.max_tree_bytes:
        logger.warning(
            "skipping %s upload: %d bytes exceeds sync.max_tree_bytes (%d)",
            label,
            plan.total_bytes,
            limits.max_tree_bytes,
        )
        return SyncStats(skipped_trees=1)

    ensure_container_dirs(
        sdk,
        [container_root, *(container_root_for(item, host_root, container_root) for item in plan.dirs)],
        user=user,
    )

    files = 0
    transferred = 0
    skipped = 0
    for host_path, size in plan.files:
        if size > limits.max_file_bytes:
            logger.warning(
                "skipping %s upload of %s: %d bytes exceeds sync.max_file_bytes (%d)",
                label,
                host_path,
                size,
                limits.max_file_bytes,
            )
            skipped += 1
            continue
        container_path = container_root_for(host_path, host_root, container_root)
        data = host_path.read_bytes()
        _write_bytes(sdk, container_path, data, user=user)
        files += 1
        transferred += len(data)
        if transferred > limits.max_tree_bytes:
            logger.warning(
                "abandoning %s upload: transferred more than sync.max_tree_bytes (%d)",
                label,
                limits.max_tree_bytes,
            )
            return SyncStats(files=files, bytes=transferred, skipped_files=skipped, skipped_trees=1)
    return SyncStats(files=files, bytes=transferred, skipped_files=skipped)


def download_tree(
    sdk: SdkSandbox,
    *,
    host_root: Path,
    container_root: str,
    limits: SyncLimits,
    user: str,
    label: str,
) -> SyncStats:
    """Download one container tree into a host directory.

    Download failures are warnings, never fatal: losing a copy of an artifact is
    better than killing a sandbox that may still be in use.  Existing host files
    are overwritten by the same name; host-only files are preserved.
    """

    root = Path(host_root)
    root.mkdir(parents=True, exist_ok=True)
    stats = _download_dir(
        sdk,
        container_root,
        root,
        limits=limits,
        user=user,
        label=label,
        depth=0,
    )
    if stats.skipped_trees:
        logger.warning(
            "abandoning %s download: tree exceeds sync.max_tree_bytes (%d)",
            label,
            limits.max_tree_bytes,
        )
    return stats


def _download_dir(
    sdk: SdkSandbox,
    container_dir: str,
    host_dir: Path,
    *,
    limits: SyncLimits,
    user: str,
    label: str,
    depth: int,
) -> SyncStats:
    if depth > 64:
        logger.warning("skipping %s download: directory nesting is too deep", label)
        return SyncStats(skipped_trees=1)
    try:
        entries = sdk.files.list(container_dir, user=user)
    except Exception as exc:
        if depth == 0 and is_missing_path_error(exc):
            return SyncStats()
        logger.warning("failed to list %s for %s download: %s", container_dir, label, exc)
        return SyncStats(skipped_trees=1 if depth == 0 else 0, skipped_files=1 if depth else 0)

    files = 0
    transferred = 0
    skipped_files = 0
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip("/")
        if not name or name in (".", "..") or not _SAFE_NAME.match(name):
            logger.warning("skipping malformed entry %r while downloading %s", entry, label)
            continue
        container_path = posixpath.join(container_dir.rstrip("/") or "/", name)
        if _is_directory_entry(entry):
            (host_dir / name).mkdir(parents=True, exist_ok=True)
            child = _download_dir(
                sdk,
                container_path,
                host_dir / name,
                limits=limits,
                user=user,
                label=label,
                depth=depth + 1,
            )
            files += child.files
            transferred += child.bytes
            skipped_files += child.skipped_files
            if child.skipped_trees:
                return SyncStats(
                    files=files,
                    bytes=transferred,
                    skipped_files=skipped_files,
                    skipped_trees=1,
                )
            continue

        size = _entry_size(entry)
        if size is None:
            try:
                probe = sdk.files.stat(container_path, user=user)
                size = _entry_size(probe) if isinstance(probe, dict) else None
            except Exception:
                size = None
        if size is not None and size > limits.max_file_bytes:
            logger.warning(
                "skipping %s download of %s: %d bytes exceeds sync.max_file_bytes (%d)",
                label,
                container_path,
                size,
                limits.max_file_bytes,
            )
            skipped_files += 1
            continue
        try:
            data = sdk.files.read(container_path, format="bytes", user=user)
            if not isinstance(data, bytes):
                data = bytes(data)
            (host_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (host_dir / name).write_bytes(data)
        except Exception as exc:
            logger.warning("failed to download %s for %s: %s", container_path, label, exc)
            skipped_files += 1
            continue
        files += 1
        transferred += len(data)
        if transferred > limits.max_tree_bytes:
            logger.warning(
                "abandoning %s download: more than sync.max_tree_bytes (%d)",
                label,
                limits.max_tree_bytes,
            )
            return SyncStats(
                files=files,
                bytes=transferred,
                skipped_files=skipped_files,
                skipped_trees=1,
            )
    return SyncStats(files=files, bytes=transferred, skipped_files=skipped_files)


class LazySkillUploader:
    """Upload read-only skill files only when a tool asks for them.

    The uploaded set is per sandbox handle: a new sandbox starts empty, which is
    correct because it starts with an empty filesystem too.  A subtree upload is
    recorded once and later reads under it stay inside the sandbox.
    """

    def __init__(
        self,
        sdk: SdkSandbox,
        *,
        sources: Sequence[SkillSource],
        limits: SyncLimits,
        user: str,
    ) -> None:
        self._sdk = sdk
        self._sources = tuple(
            sorted(sources, key=lambda item: len(item.container_root), reverse=True)
        )
        self._limits = limits
        self._user = user
        self._uploaded: set[str] = set()
        self._lock = threading.RLock()

    @property
    def sources(self) -> tuple[SkillSource, ...]:
        return self._sources

    def ensure_file(self, container_path: str) -> None:
        """Upload one skill file if it is missing from this sandbox."""

        with self._lock:
            if self._is_covered(container_path):
                return
            source, host_path = self._resolve(container_path)
            if source is None or host_path is None or not host_path.is_file():
                return
            self._upload_one(host_path, container_path)

    def ensure_tree(self, container_path: str) -> None:
        """Upload the subtree a listing or search is about to walk."""

        with self._lock:
            if self._is_covered(container_path):
                return
            source, host_path = self._resolve(container_path)
            if source is None or host_path is None:
                return
            if host_path.is_file():
                self._upload_one(host_path, container_path)
                return
            if not host_path.is_dir():
                return
            plan = _scan_tree(host_path)
            if plan is None:
                return
            if plan.total_bytes > self._limits.max_tree_bytes:
                raise SkillUploadTooLarge(
                    _too_large_message(
                        container_path, plan.total_bytes, "sync.max_tree_bytes"
                    )
                )
            for host_file, size in plan.files:
                if size > self._limits.max_file_bytes:
                    raise SkillUploadTooLarge(
                        _too_large_message(
                            container_root_for(host_file, host_path, container_path),
                            size,
                            "sync.max_file_bytes",
                        )
                    )
            ensure_container_dirs(
                self._sdk,
                [container_path, *(container_root_for(item, host_path, container_path) for item in plan.dirs)],
                user=self._user,
            )
            for host_file, _size in plan.files:
                target = container_root_for(host_file, host_path, container_path)
                if not self._is_covered(target):
                    self._upload_one(host_file, target, ensure_parent=False)
            self._uploaded.add(_normalize(container_path))

    def _upload_one(
        self, host_path: Path, container_path: str, *, ensure_parent: bool = True
    ) -> None:
        size = host_path.stat().st_size
        if size > self._limits.max_file_bytes:
            raise SkillUploadTooLarge(
                _too_large_message(container_path, size, "sync.max_file_bytes")
            )
        data = host_path.read_bytes()
        if ensure_parent:
            parent = posixpath.dirname(container_path.rstrip("/"))
            if parent and parent != "/":
                ensure_container_dirs(self._sdk, [parent], user=self._user)
        _write_bytes(self._sdk, container_path, data, user=self._user)
        self._uploaded.add(_normalize(container_path))

    def _resolve(self, container_path: str) -> tuple[SkillSource | None, Path | None]:
        normalized = _normalize(container_path)
        for source in self._sources:
            root = _normalize(source.container_root)
            if normalized == root:
                return source, source.host_root
            if normalized.startswith(root + "/"):
                relative = normalized[len(root) + 1 :]
                parts = tuple(PurePosixPath(relative).parts)
                return source, source.host_root.joinpath(*parts)
        return None, None

    def _is_covered(self, container_path: str) -> bool:
        normalized = _normalize(container_path)
        for uploaded in self._uploaded:
            if normalized == uploaded or normalized.startswith(uploaded + "/"):
                return True
        return False


def container_root_for(host_path: Path, host_root: Path, container_root: str) -> str:
    """Map one host path under host_root to its sandbox path."""

    relative = Path(host_path).relative_to(host_root)
    parts = [part for part in relative.parts if part not in ("", ".")]
    return posixpath.join(container_root.rstrip("/") or "/", *parts)


def _scan_tree(host_root: Path) -> _TreePlan | None:
    """List regular files and directories without following symlinks."""

    try:
        if host_root.is_file():
            size = host_root.stat().st_size
            return _TreePlan(dirs=(), files=((host_root, size),), total_bytes=size)
        if not host_root.is_dir():
            return None
    except OSError:
        return None

    dirs: list[Path] = []
    files: list[tuple[Path, int]] = []
    total = 0
    for current, dirnames, filenames in os.walk(host_root, followlinks=False):
        base = Path(current)
        dirnames[:] = [
            name
            for name in dirnames
            if not (base / name).is_symlink()
        ]
        dirs.extend(base / name for name in dirnames)
        for name in filenames:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            files.append((path, size))
            total += size
    return _TreePlan(dirs=tuple(dirs), files=tuple(files), total_bytes=total)


def _write_bytes(sdk: SdkSandbox, path: str, data: bytes, *, user: str) -> None:
    try:
        sdk.files.write(path, data, user=user)
    except Exception as exc:
        raise OSError(f"Failed to upload {path}: {exc}") from exc


def _entry_size(entry: dict[str, Any]) -> int | None:
    raw = entry.get("size")
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return None


def _is_directory_entry(entry: dict[str, Any]) -> bool:
    return "DIR" in str(entry.get("type") or "").upper()


def _normalize(path: str) -> str:
    return posixpath.normpath(path) or "/"


def _too_large_message(path: str, size: int, field: str) -> str:
    return (
        f"cannot upload skills on demand: {path} is {size} bytes, "
        f"over {field}; increase sandbox.e2b.sync limits or narrow the read"
    )


__all__ = [
    "LazySkillUploader",
    "SkillSource",
    "SkillUploadTooLarge",
    "SyncLimits",
    "SyncStats",
    "container_root_for",
    "download_tree",
    "ensure_container_dirs",
    "upload_tree",
]
