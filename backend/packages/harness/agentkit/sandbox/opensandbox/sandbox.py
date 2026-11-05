"""OpenSandboxSandbox: the Sandbox contract over one OpenSandbox sandbox.

Every method is the local/cube contract translated to OsbHandle calls: paths are
checked against the mount table before they reach the sandbox, a missing path
stays distinguishable from a failed one, and a command's non-zero exit is tool
output (exit code N), never an exception.

The data plane is a protocol, not the SDK, so this file has no HTTP and no
opensandbox import: tests drive it with a fake.
"""

from __future__ import annotations

import contextlib
import logging
import posixpath
import shlex
import threading
from collections.abc import Callable, Sequence
from typing import Iterator

from agentkit.sandbox.exceptions import SandboxError, SandboxTimeoutError
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.host_mounts import MountEntry
from agentkit.sandbox.opensandbox.client import Entry, OsbHandle, OsbTransportError
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.run_code import run_code_via_command
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.security import PathMapping, VirtualMount, assert_virtual_path

_DEFAULT_COMMAND_TIMEOUT = 600.0
_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_DEFAULT_MAX_TRANSFER_BYTES = 256 * 1024 * 1024
_MAX_READ_CHARS = 100_000
_MAX_GREP_LINE_CHARS = 400
_TRUNCATED_NOTE = "\n\n[truncated: file exceeds the read limit; narrow the line range]"
_GREP_TIMEOUT_SECONDS = 120.0
_MKDIR_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


class OpenSandboxSandbox(Sandbox):
    """One OpenSandbox sandbox behind the Sandbox interface."""

    #: Every execute_command runs in a new process; nothing persists.
    persistent_shell_sessions: bool | None = False

    #: True unless configured otherwise: either the code interpreter or the shell
    #: can run a snippet. RunCodeConfig decides whether the tool is exposed at all.
    supports_run_code: bool = True

    def __init__(
        self,
        handle: OsbHandle,
        *,
        sandbox_id: str,
        mounts: Sequence[MountEntry],
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        on_transport_failure: Callable[[], None] | None = None,
        code_interpreter: bool = False,
    ) -> None:
        self._handle = handle
        self._id = sandbox_id
        #: When the sandbox was created from the code-interpreter image, snippets
        #: go through CodeInterpreterSync; otherwise the shared shell path runs them.
        self._code_interpreter = code_interpreter
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
        # above decides what may be reached.
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
            output = self._handle.run_command(
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

    def run_code(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: float | None = None,
    ) -> str:
        limit = self.command_timeout if timeout is None else timeout
        if not self._code_interpreter:
            return run_code_via_command(self, code, language=language, timeout=limit)
        with self._data_plane_errors("run code"):
            output = self._handle.run_code(
                code, language=language, timeout=float(limit)
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
            text = self._handle.read_text(normalized)
        if start_line is not None or end_line is not None:
            text = _slice_lines(text, start_line=start_line, end_line=end_line)
        return _truncate(text)

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        normalized = self._assert_path(path, write=True)
        with get_file_operation_lock(self, normalized), self._data_plane_errors(
            "write", normalized
        ):
            if append:
                content = self._existing_text(normalized) + content
            self._ensure_parent_dir(normalized)
            self._handle.write_text(normalized, content)

    def download_file(self, path: str) -> bytes:
        normalized = self._assert_path(path)
        with self._data_plane_errors("read", normalized):
            return self._handle.read_bytes(
                normalized, max_bytes=_DEFAULT_MAX_TRANSFER_BYTES
            )

    def update_file(self, path: str, content: bytes) -> None:
        normalized = self._assert_path(path, write=True)
        with get_file_operation_lock(self, normalized), self._data_plane_errors(
            "write", normalized
        ):
            self._ensure_parent_dir(normalized)
            self._handle.write_bytes(normalized, content)

    # -- tree listing and search -----------------------------------------

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        normalized = self._assert_path(path)
        with self._data_plane_errors("list", normalized):
            entries = self._handle.list_entries(normalized, max_depth=max_depth)
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
            entries, truncated = self._handle.search(
                normalized, pattern, max_results=max_results
            )
        matches = [entry.path for entry in entries if include_dirs or not entry.is_dir]
        return matches, truncated

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
        command = _grep_command(
            normalized,
            pattern,
            glob=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
        with self._data_plane_errors("search", normalized):
            output = self._handle.run_command(
                command,
                env=None,
                timeout=_GREP_TIMEOUT_SECONDS,
                max_output_bytes=self.max_output_bytes,
            )
        return _parse_grep_output(output.text, path=normalized, max_results=max_results)

    # -- helpers ----------------------------------------------------------

    @contextlib.contextmanager
    def _data_plane_errors(self, action: str, path: str | None = None) -> Iterator[None]:
        """Report an unreachable sandbox, then keep the contract's error shape."""

        try:
            yield
        except OsbTransportError:
            # The sandbox may have been reaped. The owner decides whether to drop
            # the handle; this layer must not guess.
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
                "OpenSandbox %s transport-failure callback failed", self.id, exc_info=True
            )

    def _existing_text(self, path: str) -> str:
        """Current text for an append, or an empty string when the file is new."""

        try:
            return self._handle.read_text(path)
        except FileNotFoundError:
            return ""
        except SandboxError:
            raise
        except OSError:
            # An unreadable file is not a reason to fail the append; the write
            # below will surface a real permission problem.
            return ""

    def _ensure_parent_dir(self, path: str) -> None:
        """Create the parent directory first: the file API does not do it."""

        parent = posixpath.dirname(path.rstrip("/"))
        if not parent or parent == "/" or parent in self._mount_roots:
            # A mount point is created by the sandbox runtime itself.
            return
        self._handle.create_directories(parent)

    def _assert_path(self, path: str, *, write: bool = False) -> str:
        return assert_virtual_path(path, self._virtual_mounts, write=write)


def _grep_command(
    path: str,
    pattern: str,
    *,
    glob: str | None,
    literal: bool,
    case_sensitive: bool,
    max_results: int,
) -> str:
    """Build one bounded grep invocation.

    -I skips binaries, -r recurses and -n prints line numbers. The output is cut
    with head so a wide tree cannot flood the gateway; the parser treats
    max_results+1 lines as the truncation signal.
    """

    argv = ["grep", "-r", "-n", "-I"]
    if literal:
        argv.append("-F")
    if not case_sensitive:
        argv.append("-i")
    if glob:
        argv.extend(["--include", glob])
    argv.extend(["-e", pattern, "--", path])
    line_limit = max(1, int(max_results)) + 1
    return " ".join(shlex.quote(part) for part in argv) + f" | head -n {line_limit}"


def _parse_grep_output(
    text: str, *, path: str, max_results: int
) -> tuple[list[GrepMatch], bool]:
    matches: list[GrepMatch] = []
    truncated = False
    for line in text.splitlines():
        if not line:
            continue
        if len(matches) >= max_results:
            truncated = True
            break
        file_part, sep, rest = line.partition(":")
        if not sep:
            continue
        number_part, sep2, content = rest.partition(":")
        if not sep2:
            continue
        try:
            line_number = int(number_part)
        except ValueError:
            continue
        matches.append(
            GrepMatch(
                path=file_part,
                line_number=line_number,
                line=content[:_MAX_GREP_LINE_CHARS],
            )
        )
    del path
    return matches, truncated


def _slice_lines(
    text: str, *, start_line: int | None, end_line: int | None
) -> str:
    lines = text.splitlines()
    start = max((start_line or 1) - 1, 0)
    end = end_line if end_line is not None else len(lines)
    return "\n".join(lines[start:end])


def _truncate(text: str) -> str:
    if len(text) <= _MAX_READ_CHARS:
        return text
    return text[:_MAX_READ_CHARS] + _TRUNCATED_NOTE


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
            children.get(directory, []),
            key=lambda item: (not item.is_dir, item.name.lower()),
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


__all__ = ["OpenSandboxSandbox"]
