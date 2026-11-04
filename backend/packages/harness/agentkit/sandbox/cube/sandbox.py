"""CubeSandbox: the ``Sandbox`` contract over one SDK sandbox handle.

Commands run through ``sdk.commands.run`` with a fresh shell, so
``persistent_shell_sessions`` is explicitly ``False``. Output is bounded by a
``head -c`` wrapper and the per-call timeout is always explicit, because the
SDK client's transport timeout is meant for control-plane calls, not 600s work.

File paths are remote POSIX paths: every one is checked with
``assert_virtual_path`` against the mount list before it reaches the SDK. The
MicroVM, not this path table, is the isolation boundary for commands.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentkit.sandbox.cube.client import SdkSandbox, error_status
from agentkit.sandbox.cube.files import (
    command_exit_code,
    command_stdout,
    is_timeout_error,
    read_bytes,
    read_text,
    write_bytes,
    write_text,
)
from agentkit.sandbox.cube.mounts import MountEntry
from agentkit.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxTimeoutError,
)
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.sandbox import GrepMatch, Sandbox
from agentkit.sandbox.security import PathMapping, VirtualMount, assert_virtual_path

_DEFAULT_COMMAND_TIMEOUT = 600.0
_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
# 128 + SIGPIPE: head closed the pipe after the output cap. That is truncation,
# not a failed command.
_SIGPIPE_EXIT_CODE = 141
_GONE_STATUS_CODES = frozenset({404, 410})
_AUTH_STATUS_CODES = frozenset({401, 403})


class CubeSandbox(Sandbox):
    """One CubeSandbox MicroVM behind the ``Sandbox`` interface."""

    #: Every ``execute_command`` starts a new shell; exports and cwd do not persist.
    persistent_shell_sessions: bool | None = False

    def __init__(
        self,
        sdk: SdkSandbox,
        *,
        sandbox_id: str,
        mounts: Sequence[MountEntry],
        sandbox_user: str = "root",
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._sdk = sdk
        self._id = sandbox_id
        self.sandbox_user = sandbox_user
        self.command_timeout = command_timeout
        self.max_output_bytes = max_output_bytes
        self._mounts = tuple(mounts)
        self._virtual_mounts = [
            VirtualMount(container_path=entry.container_path, read_only=entry.read_only)
            for entry in self._mounts
        ]
        # ``mask_host_paths`` only redacts tool output and errors; it never decides
        # what a path is allowed to reach.
        self.path_mappings: list[PathMapping] = [
            PathMapping(
                container_path=entry.container_path,
                local_path=entry.host_path,
                read_only=entry.read_only,
            )
            for entry in self._mounts
        ]

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
            raise _map_command_error(exc, self.id, limit) from exc

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
        try:
            return read_text(
                self._sdk,
                normalized,
                user=self.sandbox_user,
                start_line=start_line,
                end_line=end_line,
            )
        except (SandboxError, OSError):
            raise
        except Exception as exc:
            raise OSError(f"Failed to read {normalized}: {exc}") from exc

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        normalized = self._assert_path(path, write=True)
        lock = get_file_operation_lock(self, normalized)
        with lock:
            try:
                write_text(
                    self._sdk,
                    normalized,
                    content,
                    user=self.sandbox_user,
                    append=append,
                )
            except (SandboxError, OSError):
                raise
            except Exception as exc:
                raise OSError(f"Failed to write {normalized}: {exc}") from exc

    def download_file(self, path: str) -> bytes:
        normalized = self._assert_path(path)
        try:
            return read_bytes(self._sdk, normalized, user=self.sandbox_user)
        except (SandboxError, OSError):
            raise
        except Exception as exc:
            raise OSError(f"Failed to read {normalized}: {exc}") from exc

    def update_file(self, path: str, content: bytes) -> None:
        normalized = self._assert_path(path, write=True)
        lock = get_file_operation_lock(self, normalized)
        with lock:
            try:
                write_bytes(self._sdk, normalized, content, user=self.sandbox_user)
            except (SandboxError, OSError):
                raise
            except Exception as exc:
                raise OSError(f"Failed to write {normalized}: {exc}") from exc

    # -- tree listing and search -----------------------------------------

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        del path, max_depth
        raise NotImplementedError("CubeSandbox.list_dir is not implemented yet")

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        del path, pattern, include_dirs, max_results
        raise NotImplementedError("CubeSandbox.glob is not implemented yet")

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
        del path, pattern, glob, literal, case_sensitive, max_results
        raise NotImplementedError("CubeSandbox.grep is not implemented yet")

    # -- helpers ----------------------------------------------------------

    def _assert_path(self, path: str, *, write: bool = False) -> str:
        return assert_virtual_path(path, self._virtual_mounts, write=write)


def _wrap_command(command: str, max_output_bytes: int) -> str:
    """Bound stdout and report the command's own status, not ``head``'s.

    The body sits in a brace group so ``2>&1`` covers the whole command. Then
    ``pipefail`` plus ``PIPESTATUS[0]`` keep ``head``'s early close from hiding a
    real failure. The ``:-$?`` fallback covers a shell that does not report
    ``PIPESTATUS``: with ``pipefail`` on, the pipeline's own status carries the
    same exit code.
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
    status = error_status(exc)
    if is_timeout_error(exc):
        return SandboxTimeoutError(f"Command timed out after {timeout:g}s")
    if status in _GONE_STATUS_CODES:
        return SandboxNotFoundError(
            f"CubeSandbox {sandbox_id} no longer exists; acquire a new sandbox",
            sandbox_id=sandbox_id,
        )
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            "CubeSandbox rejected the command credentials; check sandbox.cube.api_key"
        )
    return SandboxRuntimeError(f"CubeSandbox command failed: {exc}")


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = ["CubeSandbox"]
