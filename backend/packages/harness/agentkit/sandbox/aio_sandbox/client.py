"""AIO data-plane client: the only place that talks to agent-sandbox.

The provider talks to this narrow protocol, never to the SDK, so the whole
contract can be exercised with a fake. Three behaviours are deliberate:

1. One bash session per command. AIO's implicit persistent shell corrupts under
   concurrent use (upstream documented it), and this provider must not share
   shell state across calls anyway: persistent_shell_sessions is False.
2. env travels as a structured field, never inside the command string, so a
   request-scoped secret does not land in shell history or in docker inspect.
3. The image is pinned to all-in-one-sandbox 1.11.0, which has /v1/bash. A 404
   from that namespace means somebody pointed sandbox.aio.image at an older
   tag: that is an actionable deployment error, not something to paper over by
   switching to the exit-code-less /v1/shell transport.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentkit.sandbox.exceptions import SandboxRuntimeError, SandboxTimeoutError

logger = logging.getLogger(__name__)

_AUTH_STATUS_CODES = frozenset({401, 403})
_READY_PATH = "/v1/sandbox"
_IMAGE_HINT = (
    "the sandbox image does not support POST /v1/bash/exec (needs "
    "all-in-one-sandbox >= 1.9.3; 1.11.0 is recommended). Set sandbox.aio.image "
    "to a supported tag and recreate the container."
)
_READ_FAILED = "file not found or unreadable"


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """One finished command: merged text plus the shell exit code."""

    text: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class Entry:
    """One directory entry, in the shape the tree renderer wants."""

    name: str
    path: str
    is_dir: bool
    size: int | None = None


@dataclass(frozen=True, slots=True)
class GrepResult:
    """One content match, in the shape the sandbox contract wants."""

    path: str
    line_number: int
    line: str


class AioClient(Protocol):
    """The data-plane surface the AIO sandbox implementation depends on."""

    def ping(self) -> None: ...

    def command(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput: ...

    def read_text(
        self,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str: ...

    def write_text(self, path: str, content: str, *, append: bool = False) -> None: ...

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes: ...

    def write_bytes(self, path: str, data: bytes) -> None: ...

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]: ...

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]: ...

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepResult], bool]: ...


class SdkAioClient:
    """AioClient over the official agent-sandbox SDK.

    The SDK is imported lazily so a deployment that never selects this provider
    does not need the extra. The HTTP client is built here with trust_env=False:
    sandbox traffic is control-plane traffic on a private address, and a proxy
    picked up from the environment answers with a misleading 502.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        request_grace_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.request_grace_seconds = request_grace_seconds
        self._sdk: Any = None
        self._client: Any = None
        self._http: Any = None

    # -- lifecycle ------------------------------------------------------

    def ping(self) -> None:
        """Readiness probe: GET /v1/sandbox, the same one the AIO image serves."""

        response = self._http_client().get(f"{self.base_url}{_READY_PATH}")
        if response.status_code != 200:
            raise SandboxRuntimeError(
                f"AIO sandbox at {self.base_url} answered "
                f"{response.status_code} on {_READY_PATH}"
            )

    def close(self) -> None:
        http = self._http
        self._http = None
        self._client = None
        if http is not None:
            try:
                http.close()
            except Exception:  # noqa: BLE001 - closing twice must not raise
                logger.debug("closing the AIO HTTP client failed", exc_info=True)

    # -- commands -------------------------------------------------------

    def command(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput:
        client = self._sdk_client()
        session_id = str(uuid.uuid4())
        try:
            self._create_session(client, session_id)
            response = self._exec(client, session_id, command, env, timeout, max_output_bytes)
            data = getattr(response, "data", None)
            if data is None:
                raise SandboxRuntimeError("AIO bash exec returned no result")
            status = str(getattr(data, "status", "") or "")
            if status in {"pending", "running"}:
                raise SandboxRuntimeError(
                    "AIO bash exec returned a non-terminal status; the command "
                    "outcome is unknown and was not retried"
                )
            if status == "timed_out":
                raise SandboxTimeoutError(f"Command timed out after {timeout:g}s")
            if status == "killed":
                raise SandboxRuntimeError("the sandbox killed the command before it finished")
            if status and status != "completed":
                raise SandboxRuntimeError(f"AIO bash exec reported status {status!r}")
            return CommandOutput(
                text=_merge_output(
                    getattr(data, "stdout", "") or "", getattr(data, "stderr", "") or ""
                ),
                exit_code=_as_exit_code(getattr(data, "exit_code", None)),
            )
        finally:
            self._close_session(client, session_id)

    # -- files ----------------------------------------------------------

    def read_text(
        self,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        client = self._sdk_client()
        kwargs: dict[str, int] = {}
        # The API takes a 0-based first-line index and an exclusive end index,
        # while the Sandbox contract is 1-indexed and inclusive.
        if start_line is not None:
            kwargs["start_line"] = max(int(start_line) - 1, 0)
        if end_line is not None:
            kwargs["end_line"] = max(int(end_line), 0)
        try:
            result = client.file.read_file(file=path, **kwargs)
        except Exception as exc:
            raise _map_file_error(exc, path, action="read") from exc
        data = getattr(result, "data", None)
        content = getattr(data, "content", None)
        if content is None:
            raise FileNotFoundError(f"{path}: {_READ_FAILED}")
        return str(content)

    def write_text(self, path: str, content: str, *, append: bool = False) -> None:
        client = self._sdk_client()
        try:
            client.file.write_file(file=path, content=content, append=append)
        except Exception as exc:
            raise _map_file_error(exc, path, action="write") from exc

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        client = self._sdk_client()
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in client.file.download_file(path=path):
                total += len(chunk)
                if total > max_bytes:
                    raise OSError(
                        f"Failed to read {path}: it exceeds the {max_bytes}-byte "
                        "transfer limit"
                    )
                chunks.append(bytes(chunk))
        except OSError:
            raise
        except Exception as exc:
            raise _map_file_error(exc, path, action="read") from exc
        return b"".join(chunks)

    def write_bytes(self, path: str, data: bytes) -> None:
        client = self._sdk_client()
        try:
            client.file.upload_file(file=bytes(data), path=path)
        except Exception as exc:
            raise _map_file_error(exc, path, action="write") from exc

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]:
        client = self._sdk_client()
        try:
            result = client.file.list_path(
                path=path, max_depth=max_depth, include_size=True
            )
        except Exception as exc:
            raise _map_file_error(exc, path, action="list") from exc
        data = getattr(result, "data", None)
        rows = getattr(data, "files", None) or []
        entries: list[Entry] = []
        for row in rows:
            is_dir = bool(getattr(row, "is_directory", False))
            row_path = str(getattr(row, "path", "") or "")
            if not is_dir and row_path.rstrip("/") == path.rstrip("/"):
                # The server answered with the file itself: not a directory.
                raise NotADirectoryError(f"{path}: not a directory")
            entries.append(
                Entry(
                    name=str(getattr(row, "name", "") or ""),
                    path=row_path,
                    is_dir=is_dir,
                    size=getattr(row, "size", None),
                )
            )
        return entries

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        client = self._sdk_client()
        try:
            result = client.file.glob_files(
                path=path,
                pattern=pattern,
                max_results=max_results,
                files_only=not include_dirs,
            )
        except Exception as exc:
            raise _map_file_error(exc, path, action="search") from exc
        data = getattr(result, "data", None)
        rows = getattr(data, "files", None) or []
        matches = [str(getattr(row, "path", "") or "") for row in rows]
        return [match for match in matches if match], bool(getattr(data, "truncated", False))

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepResult], bool]:
        client = self._sdk_client()
        kwargs: dict[str, Any] = {
            "path": path,
            "pattern": pattern,
            "max_results": max_results,
            "fixed_strings": bool(literal),
            "case_insensitive": not case_sensitive,
        }
        if glob:
            kwargs["include"] = [glob]
        try:
            result = client.file.grep_files(**kwargs)
        except Exception as exc:
            raise _map_file_error(exc, path, action="search") from exc
        data = getattr(result, "data", None)
        rows = getattr(data, "matches", None) or []
        matches = [
            GrepResult(
                path=str(getattr(row, "file", "") or ""),
                line_number=int(getattr(row, "line_number", 0) or 0),
                line=str(getattr(row, "line_content", "") or ""),
            )
            for row in rows
        ]
        return matches, bool(getattr(data, "truncated", False))

    # -- internals ------------------------------------------------------

    def _create_session(self, client: Any, session_id: str) -> None:
        try:
            client.bash.create_session(session_id=session_id)
        except Exception as exc:
            raise _map_command_error(exc, "start a command session") from exc

    def _exec(
        self,
        client: Any,
        session_id: str,
        command: str,
        env: Mapping[str, str] | None,
        timeout: float,
        max_output_bytes: int,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "command": command,
            "session_id": session_id,
            "hard_timeout": float(timeout),
            "max_output_length": int(max_output_bytes),
            "request_options": {
                "timeout_in_seconds": float(timeout) + self.request_grace_seconds
            },
        }
        if env:
            kwargs["env"] = dict(env)
        try:
            return client.bash.exec(**kwargs)
        except Exception as exc:
            raise _map_command_error(exc, "run a command") from exc

    def _close_session(self, client: Any, session_id: str) -> None:
        try:
            client.bash.close_session(session_id)
        except Exception:  # noqa: BLE001 - a leaked session must not mask the result
            logger.warning("could not release AIO bash session %s", session_id, exc_info=True)

    def _sdk_client(self) -> Any:
        if self._client is None:
            import agent_sandbox

            self._client = agent_sandbox.Sandbox(
                base_url=self.base_url,
                timeout=self.timeout,
                httpx_client=self._http_client(),
            )
        return self._client

    def _http_client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(
                timeout=self.timeout,
                trust_env=False,
                follow_redirects=True,
            )
        return self._http


def _merge_output(stdout: str, stderr: str) -> str:
    text = stdout or ""
    if stderr:
        text = f"{text}\n{stderr}".strip() if text else stderr
    return text.strip()


def _as_exit_code(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


def _status_code(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None)
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _type_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _map_command_error(exc: BaseException, action: str) -> Exception:
    if isinstance(exc, (SandboxRuntimeError, SandboxTimeoutError)):
        return exc
    names = _type_names(exc)
    if "TimeoutException" in names or "TimeoutError" in names:
        return SandboxTimeoutError(f"the sandbox {action}: request timed out")
    status = _status_code(exc)
    if status == 404:
        return SandboxRuntimeError(f"could not {action}: {_IMAGE_HINT}")
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            f"could not {action}: the sandbox rejected the request; check the "
            "AIO image's auth settings"
        )
    return SandboxRuntimeError(f"could not {action}: {exc}")


def _map_file_error(exc: BaseException, path: str, *, action: str) -> Exception:
    if isinstance(exc, OSError):
        return exc
    names = _type_names(exc)
    # The server reports read failures as an error envelope the SDK cannot
    # parse, so a pydantic ValidationError means "missing or unreadable".
    if "ValidationError" in names:
        return FileNotFoundError(f"{path}: {_READ_FAILED}")
    if names & {"FileNotFoundException", "NotFoundException"}:
        return FileNotFoundError(f"{path}: no such file or directory")
    if "TimeoutException" in names or "TimeoutError" in names:
        return SandboxTimeoutError(f"the sandbox file API timed out while trying to {action} {path}")
    status = _status_code(exc)
    if status == 404:
        return FileNotFoundError(f"{path}: no such file or directory")
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            f"the sandbox rejected the request while trying to {action} {path}"
        )
    return OSError(f"Failed to {action} {path}: {exc}")


__all__ = [
    "AioClient",
    "CommandOutput",
    "Entry",
    "GrepResult",
    "SdkAioClient",
]
