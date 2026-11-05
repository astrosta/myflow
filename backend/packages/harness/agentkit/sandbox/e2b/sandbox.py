"""E2BSandbox: the Sandbox contract over one E2B SDK sandbox handle.

Commands run through sdk.commands.run with a fresh shell, so
persistent_shell_sessions is explicitly False.  Output is bounded by a head -c
wrapper and the per-call timeout is always explicit, because the SDK client's
transport timeout is meant for control-plane calls, not 600s work.

File paths are remote POSIX paths checked against a static virtual mount table.
There is no host mount: the sandbox filesystem is authoritative, and skill
sources are uploaded lazily by LazySkillUploader when a tool first reads them.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Sequence

from agentkit.sandbox.e2b.client import (
    SandboxGoneError,
    SdkSandbox,
    error_status,
    is_gone_error,
)
from agentkit.sandbox.e2b.files import (
    command_exit_code,
    command_stdout,
    glob_paths,
    grep_paths,
    is_timeout_error,
    list_tree,
    read_bytes,
    read_text,
    write_bytes,
    write_text,
)
from agentkit.sandbox.e2b.sync import LazySkillUploader, SkillSource, SyncLimits
from agentkit.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxTimeoutError,
)
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.run_code import run_code_via_command
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.security import PathMapping, VirtualMount, assert_virtual_path

_DEFAULT_COMMAND_TIMEOUT = 600.0
_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
# 128 + SIGPIPE: head closed the pipe after the output cap. That is truncation,
# not a failed command.
_SIGPIPE_EXIT_CODE = 141
_AUTH_STATUS_CODES = frozenset({401, 403})

logger = logging.getLogger(__name__)


class E2BSandbox(Sandbox):
    """One E2B sandbox behind the Sandbox interface."""

    #: Every execute_command starts a new shell; exports and cwd do not persist.
    persistent_shell_sessions: bool | None = False

    #: The E2B template ships python3, so a snippet can run through the shell.
    supports_run_code: bool = True

    def __init__(
        self,
        sdk: SdkSandbox,
        *,
        sandbox_id: str,
        path_mappings: Sequence[PathMapping],
        skill_sources: Sequence[SkillSource] = (),
        sync_limits: SyncLimits | None = None,
        sandbox_user: str = "root",
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        on_dead: Callable[[], None] | None = None,
    ) -> None:
        self._sdk = sdk
        self._id = sandbox_id
        self._on_dead = on_dead
        self._dead_notified = False
        self._dead_lock = threading.Lock()
        self.sandbox_user = sandbox_user
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes
        # The static table is the whole path guard. local_path is only used by
        # mask_host_paths to redact host roots from tool output.
        self.path_mappings: list[PathMapping] = list(path_mappings)
        self._virtual_mounts = [
            VirtualMount(container_path=mapping.container_path, read_only=mapping.read_only)
            for mapping in self.path_mappings
        ]
        self._skills = (
            LazySkillUploader(
                sdk,
                sources=skill_sources,
                limits=sync_limits or SyncLimits(),
                user=sandbox_user,
            )
            if skill_sources
            else None
        )

    # -- commands ---------------------------------------------------------

    def _execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        limit = self.command_timeout if timeout is None else timeout
        wrapped = _wrap_command(command, self.max_output_bytes)
        try:
            result = self._sdk.commands.run(
                wrapped,
                timeout=limit,
                envs=env,
                user=self.sandbox_user,
            )
        except Exception as exc:
            mapped = _map_command_error(exc, self.id, limit)
            if isinstance(mapped, SandboxNotFoundError):
                # A reaped sandbox answers every later call the same way, so the
                # owner must stop handing this handle out now.
                self._notify_dead()
            if mapped is exc:
                raise
            raise mapped from exc

        stdout = command_stdout(result)
        stderr = _as_text(getattr(result, "stderr", ""))
        code = command_exit_code(result)
        if code == _SIGPIPE_EXIT_CODE:
            code = 0
        text = stdout if not stderr else f"{stdout}\n{stderr}".strip()
        if code != 0:
            text = f"{text.rstrip()}\nexit code {code}"
        return mask_host_paths(text, self)

    # -- files ------------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        normalized = self._assert_path(path)
        with self._file_errors(normalized, "read"):
            self._ensure_skill_file(normalized)
            return read_text(
                self._sdk,
                normalized,
                user=self.sandbox_user,
                start_line=start_line,
                end_line=end_line,
            )

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        normalized = self._assert_path(path, write=True)
        lock = get_file_operation_lock(self, normalized)
        with lock, self._file_errors(normalized, "write"):
            write_text(
                self._sdk,
                normalized,
                content,
                user=self.sandbox_user,
                append=append,
            )

    def download_file(self, path: str) -> bytes:
        normalized = self._assert_path(path)
        with self._file_errors(normalized, "read"):
            self._ensure_skill_file(normalized)
            return read_bytes(self._sdk, normalized, user=self.sandbox_user)

    def update_file(self, path: str, content: bytes) -> None:
        normalized = self._assert_path(path, write=True)
        lock = get_file_operation_lock(self, normalized)
        with lock, self._file_errors(normalized, "write"):
            write_bytes(self._sdk, normalized, content, user=self.sandbox_user)

    # -- tree listing and search -----------------------------------------

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        normalized = self._assert_path(path)
        with self._file_errors(normalized, "list"):
            self._ensure_skill_tree(normalized)
            return list_tree(
                self._sdk, normalized, user=self.sandbox_user, max_depth=max_depth
            )

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        normalized = self._assert_path(path)
        with self._file_errors(normalized, "search"):
            self._ensure_skill_tree(normalized)
            return glob_paths(
                self._sdk,
                normalized,
                pattern,
                user=self.sandbox_user,
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
        with self._file_errors(normalized, "search"):
            self._ensure_skill_tree(normalized)
            return grep_paths(
                self._sdk,
                normalized,
                pattern,
                user=self.sandbox_user,
                glob=glob,
                literal=literal,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )

    # -- helpers ----------------------------------------------------------

    def _ensure_skill_file(self, path: str) -> None:
        if self._skills is not None:
            self._skills.ensure_file(path)

    def _ensure_skill_tree(self, path: str) -> None:
        if self._skills is not None:
            self._skills.ensure_tree(path)

    def _notify_dead(self) -> None:
        """Report that the remote sandbox is gone, at most once per handle.

        Commands may run concurrently, so the owner gets exactly one callback
        even when several calls observe 404/410. A failing callback must never
        hide the SandboxNotFoundError the caller needs in order to re-acquire.
        """

        with self._dead_lock:
            if self._dead_notified:
                return
            self._dead_notified = True
        if self._on_dead is None:
            return
        try:
            self._on_dead()
        except Exception:
            logger.warning(
                "E2BSandbox %s on_dead callback failed; the handle is still dead",
                self.id,
                exc_info=True,
            )

    @contextlib.contextmanager
    def _file_errors(self, path: str, action: str) -> Iterator[None]:
        """Keep the file contract while still reporting a gone sandbox.

        A file helper maps its own failures, but the handle owner must stop
        serving a sandbox that no longer exists, so a gone sandbox is reported
        before the generic OSError wrapper hides it.
        """

        try:
            yield
        except SandboxGoneError:
            self._notify_dead()
            raise
        except (SandboxError, OSError):
            raise
        except Exception as exc:
            raise OSError(f"Failed to {action} {path}: {exc}") from exc

    def run_code(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: float | None = None,
    ) -> str:
        """Run a snippet through the sandbox shell (the template has python3)."""

        return run_code_via_command(self, code, language=language, timeout=timeout)

    def _assert_path(self, path: str, *, write: bool = False) -> str:
        return assert_virtual_path(path, self._virtual_mounts, write=write)


def _wrap_command(command: str, max_output_bytes: int) -> str:
    """Bound stdout and report the command's own status, not head's.

    The body sits in a brace group so 2>&1 covers the whole command. Then
    pipefail plus PIPESTATUS[0] keep head's early close from hiding a real
    failure. The :-$? fallback covers a shell that does not report PIPESTATUS:
    with pipefail on, the pipeline's own status carries the same exit code.
    """

    return (
        "set -o pipefail\n"
        f"{{ {command}\n"
        f"}} 2>&1 | head -c {max_output_bytes}\n"
        "status=${PIPESTATUS[0]:-$?}\n"
        'if [ "$status" -eq 141 ]; then status=0; fi\n'
        'exit "$status"'
    )


def _map_command_error(
    exc: BaseException, sandbox_id: str, timeout: float
) -> Exception:
    if isinstance(
        exc, (SandboxNotFoundError, SandboxRuntimeError, SandboxTimeoutError)
    ):
        return exc
    if isinstance(exc, SandboxGoneError) or is_gone_error(exc):
        return SandboxNotFoundError(
            f"E2BSandbox {sandbox_id} no longer exists; acquire a new sandbox",
            sandbox_id=sandbox_id,
        )
    status = error_status(exc)
    if is_timeout_error(exc):
        return SandboxTimeoutError(f"Command timed out after {timeout:g}s")
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            "E2BSandbox rejected the command credentials; check sandbox.e2b.api_key"
        )
    return SandboxRuntimeError(f"E2BSandbox command failed: {exc}")


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = ["E2BSandbox"]
