"""Local ``Sandbox``: path translation and process management. Not an isolation boundary."""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import threading

from agentkit.sandbox.env_policy import build_sandbox_env
from agentkit.sandbox.exceptions import SandboxTimeoutError
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.local.list_dir import render_tree
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.search import glob_search, grep_search
from agentkit.sandbox.security import (
    PathMapping,
    assert_writable,
    find_path_mapping,
    resolve_sandbox_path,
)

_MAX_READ_CHARS = 100_000
_MAX_OUTPUT_BYTES = 1024 * 1024
_OUTPUT_GRACE_SECONDS = 0.2

# Exclude virtual prefixes from MSYS conversion. Do not set MSYS_NO_PATHCONV.
_MSYS_EXCL = "/mnt/user-data;/mnt/skills;/mnt/integrations"


class LocalSandbox(Sandbox):
    """Host filesystem behind virtual paths. ``persistent_shell_sessions`` is explicitly False.

    Once host bash is enabled, the shell can ignore ``PathMapping``. File-tool checks
    are real; the shell boundary is not.
    """

    persistent_shell_sessions: bool | None = False

    def __init__(self, sandbox_id: str, mappings: list[PathMapping], *, command_timeout: float = 600) -> None:
        self._id = sandbox_id
        self.path_mappings = list(mappings)
        self.command_timeout = command_timeout
        self._processes: list[subprocess.Popen[bytes]] = []
        self._processes_guard = threading.Lock()

    def _execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        merged = build_sandbox_env(env)
        if os.name == "nt":
            merged["MSYS2_ARG_CONV_EXCL"] = _MSYS_EXCL
        limit = self.command_timeout if timeout is None else timeout
        argv = _shell_argv(command)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged,
            start_new_session=(os.name != "nt"),
            creationflags=_windows_creationflags(),
        )
        with self._processes_guard:
            self._processes.append(proc)
        assert proc.stdout is not None and proc.stderr is not None
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        _drain_pipes(proc, stdout_buf, stderr_buf, limit)
        output = _decode_output(bytes(stdout_buf))
        error = _decode_output(bytes(stderr_buf))
        text = output if not error else f"{output}\n{error}".strip()
        if proc.returncode is None:
            _kill_process_tree(proc)
            raise SandboxTimeoutError(f"Command timed out after {limit}s")
        if proc.returncode != 0:
            text = f"{text}\nexit code {proc.returncode}".strip()
        return mask_host_paths(text, self)

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        host = resolve_sandbox_path(path, self.path_mappings)
        try:
            text = host.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise OSError(f"Failed to read {path}") from err
        if start_line is not None or end_line is not None:
            lines = text.splitlines()
            start = max((start_line or 1) - 1, 0)
            end = end_line if end_line is not None else len(lines)
            text = "\n".join(lines[start:end])
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + "\n\n[truncated: file exceeds the read limit; narrow the line range]"
        return text

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        host = assert_writable(path, self.path_mappings)
        lock = get_file_operation_lock(self, path)
        with lock:
            host.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with host.open(mode, encoding="utf-8", newline="\n") as handle:
                handle.write(content)

    def download_file(self, path: str) -> bytes:
        host = resolve_sandbox_path(path, self.path_mappings)
        try:
            return host.read_bytes()
        except OSError as err:
            raise OSError(f"Failed to read {path}") from err

    def update_file(self, path: str, content: bytes) -> None:
        host = assert_writable(path, self.path_mappings)
        lock = get_file_operation_lock(self, path)
        with lock:
            host.parent.mkdir(parents=True, exist_ok=True)
            host.write_bytes(content)

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        host = resolve_sandbox_path(path, self.path_mappings)
        if not host.exists():
            raise FileNotFoundError(path)
        try:
            return render_tree(host, max_depth=max_depth)
        except FileNotFoundError as err:
            raise FileNotFoundError(path) from err
        except OSError as err:
            raise OSError(f"Failed to list {path}") from err

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        host = resolve_sandbox_path(path, self.path_mappings)
        return glob_search(
            host,
            path.rstrip("/"),
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
        host = resolve_sandbox_path(path, self.path_mappings)
        return grep_search(
            host,
            path.rstrip("/"),
            pattern,
            glob=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    def shutdown(self) -> None:
        with self._processes_guard:
            processes = list(self._processes)
            self._processes.clear()
        for proc in processes:
            if proc.poll() is None:
                _kill_process_tree(proc)


def _shell_argv(command: str) -> list[str]:
    # ``set -m`` returns without waiting for background jobs. Their output is drained, not stored.
    wrapped = f"set -m\n{command}"
    return ["bash", "-lc", wrapped]


def _windows_creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _decode_output(data: bytes) -> str:
    if os.name == "nt":
        text = data.decode(locale.getpreferredencoding(False), errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return data.decode("utf-8", errors="replace")


def _read_bounded(stream, buf: bytearray) -> None:
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        if len(buf) < _MAX_OUTPUT_BYTES:
            room = _MAX_OUTPUT_BYTES - len(buf)
            buf.extend(chunk[:room])


def _drain_pipes(
    proc: subprocess.Popen[bytes],
    stdout_buf: bytearray,
    stderr_buf: bytearray,
    timeout: float,
) -> None:
    """Drain both pipes on threads so a full pipe cannot deadlock the child."""
    assert proc.stdout is not None and proc.stderr is not None
    readers = [
        threading.Thread(target=_read_bounded, args=(proc.stdout, stdout_buf), daemon=True),
        threading.Thread(target=_read_bounded, args=(proc.stderr, stderr_buf), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return
    for reader in readers:
        reader.join(timeout=_OUTPUT_GRACE_SECONDS)


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def virtual_root_for(path: str, mappings: list[PathMapping]) -> str:
    mapping = find_path_mapping(path, mappings)
    return mapping.container_path if mapping is not None else path
