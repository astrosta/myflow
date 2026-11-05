"""AioSandbox: the Sandbox contract over one AIO container.

Every method here is the local/cube contract translated to AioClient calls:
paths are checked against the mount table before they reach the container, a
missing path stays distinguishable from a failed one, and a command's non-zero
exit is tool output (exit code N), never an exception.

The data plane is a protocol, not the SDK, so this file has no HTTP and no
agent-sandbox import: tests drive it with a fake.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import shlex
import threading
from collections.abc import Callable, Sequence
from typing import Iterator

from agentkit.sandbox.aio_sandbox.client import AioClient, AioTransportError, Entry
from agentkit.sandbox.exceptions import SandboxError, SandboxNotFoundError
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.host_mounts import MountEntry
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.security import PathMapping, VirtualMount, assert_virtual_path

_DEFAULT_COMMAND_TIMEOUT = 600.0
_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_DEFAULT_MAX_TRANSFER_BYTES = 256 * 1024 * 1024
_MAX_READ_CHARS = 100_000
_TRUNCATED_NOTE = "\n\n[truncated: file exceeds the read limit; narrow the line range]"
_MKDIR_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


class AioSandbox(Sandbox):
    """One all-in-one-sandbox container behind the Sandbox interface."""

    #: Every execute_command runs in its own bash session; nothing persists.
    persistent_shell_sessions: bool | None = False

    def __init__(
        self,
        client: AioClient,
        *,
        sandbox_id: str,
        mounts: Sequence[MountEntry],
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        on_transport_failure: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._id = sandbox_id
        self._on_transport_failure = on_transport_failure
        self._transport_notified = False
        self._transport_lock = threading.Lock()
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes
        self._mounts = tuple(mounts)
        self._mount_roots = frozenset(
            entry.container_path.rstrip("/") or "/" for entry in self._mounts
        )
        self._virtual_mounts = [
            VirtualMount(container_path=entry.container_path, read_only=entry.read_only)
            for entry in self._mounts
        ]
        # mask_host_paths only redacts host paths in tool output; the path guard
        # above is what decides what may be reached.
        self.path_mappings: list[PathMapping] = [
            PathMapping(
                container_path=entry.container_path,
                local_path=entry.host_path,
                read_only=entry.read_only,
            )
            for entry in self._mounts
        ]

    # -- commands -------------------------------------------------------

    def _execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        limit = self.command_timeout if timeout is None else timeout
        with self._data_plane_errors("run a command"):
            output = self._client.command(
                command,
                env=env,
                timeout=limit,
                max_output_bytes=self.max_output_bytes,
            )
        text = output.text
        if output.exit_code != 0:
            text = (
                f"{text.rstrip()}\nexit code {output.exit_code}"
                if text
                else f"exit code {output.exit_code}"
            )
        return mask_host_paths(text, self)

    # -- files ----------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        normalized = self._assert_path(path)
        with self._data_plane_errors("read", normalized):
            text = self._client.read_text(
                normalized, start_line=start_line, end_line=end_line
            )
        return _truncate(text)

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        normalized = self._assert_path(path, write=True)
        with get_file_operation_lock(self, normalized), self._data_plane_errors(
            "write", normalized
        ):
            self._ensure_parent_dir(normalized)
            self._client.write_text(normalized, content, append=append)

    def download_file(self, path: str) -> bytes:
        normalized = self._assert_path(path)
        with self._data_plane_errors("read", normalized):
            return self._client.read_bytes(normalized, max_bytes=_DEFAULT_MAX_TRANSFER_BYTES)

    def update_file(self, path: str, content: bytes) -> None:
        normalized = self._assert_path(path, write=True)
        with get_file_operation_lock(self, normalized), self._data_plane_errors(
            "write", normalized
        ):
            self._ensure_parent_dir(normalized)
            self._client.write_bytes(normalized, content)

    # -- tree listing and search -----------------------------------------

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        normalized = self._assert_path(path)
        with self._data_plane_errors("list", normalized):
            entries = self._client.list_entries(normalized, max_depth=max_depth)
        return _render_entries(normalized, entries, max_depth=max_depth)

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        normalized = self._assert_path(path)
        with self._data_plane_errors("search", normalized):
            return self._client.glob(
                normalized,
                pattern,
                include_dirs=include_dirs,
                max_results=max_results,
            )

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        normalized = self._assert_path(path)
        with self._data_plane_errors("search", normalized):
            matches, truncated = self._client.grep(
                normalized,
                pattern,
                glob=glob,
                literal=literal,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
        return (
            [
                GrepMatch(path=match.path, line_number=match.line_number, line=match.line)
                for match in matches
            ],
            truncated,
        )

    # -- helpers ----------------------------------------------------------

    @contextlib.contextmanager
    def _data_plane_errors(self, action: str, path: str | None = None) -> Iterator[None]:
        """Report an unreachable container, then keep the contract's error shape."""

        try:
            yield
        except AioTransportError:
            # The container may have been removed under us. The owner decides
            # whether to drop the handle; this layer must not guess.
            self._notify_transport_failure()
            raise
        except (SandboxError, OSError):
            raise
        except Exception as exc:
            target = f" {path}" if path else ""
            raise OSError(f"Failed to {action}{target}: {exc}") from exc

    def _notify_transport_failure(self) -> None:
        if self._on_transport_failure is None:
            return
        with self._transport_lock:
            if self._transport_notified:
                return
            self._transport_notified = True
        try:
            self._on_transport_failure()
        except Exception:
            logger.warning(
                "AioSandbox %s transport-failure callback failed", self.id, exc_info=True
            )

    def _ensure_parent_dir(self, path: str) -> None:
        """Create the parent directory first: the file API does not do it."""

        parent = posixpath.dirname(path.rstrip("/"))
        if not parent or parent == "/":
            return
        if parent in self._mount_roots:
            # A mount point is created by Docker itself; no round trip needed.
            return
        command = f"mkdir -p -- {shlex.quote(parent)}"
        output = self._client.command(
            command,
            env=None,
            timeout=_MKDIR_TIMEOUT_SECONDS,
            max_output_bytes=self.max_output_bytes,
        )
        if output.exit_code != 0:
            detail = output.text.strip() or f"exit code {output.exit_code}"
            raise OSError(f"Failed to create the parent directory of {path}: {detail}")

    def _assert_path(self, path: str, *, write: bool = False) -> str:
        return assert_virtual_path(path, self._virtual_mounts, write=write)


def _render_entries(root: str, entries: Sequence[Entry], *, max_depth: int) -> list[str]:
    """Render a flat listing as the same tree lines the other providers emit.

    Directories come first, then names case-insensitively, matching local and
    cube. Entries deeper than max_depth are dropped without failing the listing.
    """

    root_path = root.rstrip("/") or "/"
    children: dict[str, list[Entry]] = {}
    for entry in entries:
        parent = posixpath.dirname(entry.path.rstrip("/")) or "/"
        children.setdefault(parent, []).append(entry)

    lines: list[str] = []

    def walk(directory: str, prefix: str, depth: int) -> None:
        rows = sorted(
            children.get(directory, []), key=lambda item: (not item.is_dir, item.name.lower())
        )
        for index, row in enumerate(rows):
            last = index == len(rows) - 1
            branch = "└── " if last else "├── "
            label = f"{row.name}/" if row.is_dir else row.name
            lines.append(f"{prefix}{branch}{label}")
            if not row.is_dir or depth + 1 >= max_depth:
                continue
            extension = "    " if last else "│   "
            walk(row.path.rstrip("/"), prefix + extension, depth + 1)

    walk(root_path, "", 0)
    return lines


def _truncate(text: str) -> str:
    if len(text) <= _MAX_READ_CHARS:
        return text
    return text[:_MAX_READ_CHARS] + _TRUNCATED_NOTE


# SandboxNotFoundError is imported for callers that catch it at this boundary.
__all__ = ["AioSandbox", "SandboxNotFoundError"]
