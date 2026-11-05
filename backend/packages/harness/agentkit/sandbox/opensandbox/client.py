"""OpenSandbox data-plane client: the only place that imports the SDK.

The provider and the Sandbox implementation talk to these two protocols, so the
whole contract can be driven by a fake. The SDK is synchronous (SandboxSync),
which matches the Sandbox contract: no event loop bridge anywhere.

Three behaviours are deliberate:

1. One command per call, no shell session. Every execute_command is a fresh
   process, so persistent_shell_sessions is False and two concurrent sub-agents
   cannot corrupt each other's shell.
2. env travels in RunCommandOpts.envs, never inside the command string: a
   request-scoped secret must not land in shell history or in the sandbox env.
3. The server owns containers. This module never runs docker; it only asks the
   server to create/connect/list/kill a sandbox.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from agentkit.sandbox.exceptions import SandboxNotFoundError, SandboxRuntimeError, SandboxTimeoutError
from agentkit.sandbox.host_mounts import MountEntry

logger = logging.getLogger(__name__)

_RUNNING_STATES = frozenset({"running", "RUNNING"})
# One page is plenty: a gateway only ever looks for its own threads, and the
# server-side filter already narrows by metadata.
_PAGE_SIZE = 100


class OsbTransportError(SandboxRuntimeError, OSError):
    """The sandbox server could not be reached.

    Subclasses both families on purpose: a file operation must still look like an
    OSError to the tool layer, while the owner also needs a signal that the
    sandbox may be gone.
    """


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """One finished command: merged text plus the exit code."""

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


@dataclass(frozen=True, slots=True)
class SandboxSummary:
    """The slice of a sandbox listing the provider reasons about."""

    sandbox_id: str
    state: str
    metadata: Mapping[str, str]
    created_at: datetime | None = None
    expires_at: datetime | None = None


class OsbHandle(Protocol):
    """One sandbox: what the Sandbox implementation needs."""

    sandbox_id: str

    def is_running(self) -> bool: ...

    def run_command(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput: ...

    def read_text(self, path: str) -> str: ...

    def write_text(self, path: str, content: str) -> None: ...

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes: ...

    def write_bytes(self, path: str, data: bytes) -> None: ...

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]: ...

    def search(
        self, path: str, pattern: str, *, max_results: int
    ) -> tuple[list[Entry], bool]: ...

    def create_directories(self, path: str) -> None: ...

    def close(self) -> None: ...


class OsbClient(Protocol):
    """The control surface the provider depends on."""

    def ping(self) -> None: ...

    def create(
        self,
        *,
        image: str,
        timeout_seconds: float,
        ready_timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        metadata: Mapping[str, str] | None = None,
        mounts: Sequence[MountEntry] = (),
        resource: Mapping[str, str] | None = None,
    ) -> OsbHandle: ...

    def connect(self, sandbox_id: str) -> OsbHandle: ...

    def list_by_metadata(
        self, metadata: Mapping[str, str], *, states: Sequence[str] = ("RUNNING",)
    ) -> list[SandboxSummary]: ...

    def kill(self, sandbox_id: str) -> None: ...

    def close(self) -> None: ...


class SdkOsbHandle:
    """OsbHandle over one opensandbox.SandboxSync."""

    def __init__(self, sandbox: Any) -> None:
        self._sandbox = sandbox
        self.sandbox_id = str(getattr(sandbox, "id", "") or "")

    def is_running(self) -> bool:
        try:
            info = self._sandbox.get_info()
        except Exception as exc:
            raise _map_error(exc, "read sandbox status") from exc
        state = str(getattr(getattr(info, "status", None), "state", "") or "")
        return state.upper() in {state_name.upper() for state_name in _RUNNING_STATES}

    def run_command(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput:
        opts_kwargs: dict[str, Any] = {"timeout": timedelta(seconds=float(timeout))}
        if env:
            opts_kwargs["envs"] = dict(env)
        try:
            execution = self._sandbox.commands.run(
                command, opts=_run_opts(opts_kwargs)
            )
        except Exception as exc:
            raise _map_command_error(exc, timeout) from exc
        stdout = _join_messages(getattr(getattr(execution, "logs", None), "stdout", None))
        stderr = _join_messages(getattr(getattr(execution, "logs", None), "stderr", None))
        text = _merge_output(stdout, stderr)
        if len(text) > max_output_bytes:
            # The server already caps per stream; this is the merged-output guard.
            text = text[:max_output_bytes]
        error = getattr(execution, "error", None)
        if error and not text:
            text = str(error)
        return CommandOutput(
            text=text, exit_code=_as_exit_code(getattr(execution, "exit_code", None))
        )

    def read_text(self, path: str) -> str:
        try:
            return str(self._sandbox.files.read_file(path))
        except Exception as exc:
            raise _map_file_error(exc, path, action="read") from exc

    def write_text(self, path: str, content: str) -> None:
        try:
            self._sandbox.files.write_file(path, content, mode=0o644)
        except Exception as exc:
            raise _map_file_error(exc, path, action="write") from exc

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        try:
            data = self._sandbox.files.read_bytes(path)
        except Exception as exc:
            raise _map_file_error(exc, path, action="read") from exc
        raw = bytes(data)
        if len(raw) > max_bytes:
            raise OSError(
                f"Failed to read {path}: it exceeds the {max_bytes}-byte transfer limit"
            )
        return raw

    def write_bytes(self, path: str, data: bytes) -> None:
        try:
            self._sandbox.files.write_file(path, bytes(data))
        except Exception as exc:
            raise _map_file_error(exc, path, action="write") from exc

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]:
        try:
            rows = self._sandbox.files.list_directory(
                _directory_entry(path, max_depth)
            )
        except Exception as exc:
            raise _map_file_error(exc, path, action="list") from exc
        entries = [_to_entry(row) for row in rows or ()]
        if len(entries) == 1 and not entries[0].is_dir and _same_path(entries[0].path, path):
            # The server answered with the file itself: not a directory.
            raise NotADirectoryError(f"{path}: not a directory")
        return entries

    def search(
        self, path: str, pattern: str, *, max_results: int
    ) -> tuple[list[Entry], bool]:
        try:
            rows = self._sandbox.files.search(_search_entry(path, pattern))
        except Exception as exc:
            raise _map_file_error(exc, path, action="search") from exc
        entries = [_to_entry(row) for row in rows or ()]
        truncated = len(entries) > max_results
        return entries[:max_results], truncated

    def create_directories(self, path: str) -> None:
        try:
            self._sandbox.files.create_directories([_write_entry(path)])
        except Exception as exc:
            raise _map_file_error(exc, path, action="create") from exc

    def close(self) -> None:
        try:
            self._sandbox.close()
        except Exception:  # noqa: BLE001 - closing twice must not raise
            logger.debug("closing the OpenSandbox handle failed", exc_info=True)


class SdkOsbClient:
    """OsbClient over the official opensandbox SDK (sync flavour)."""

    def __init__(
        self,
        domain: str,
        *,
        protocol: str = "http",
        api_key: str = "",
        request_timeout: float = 30.0,
    ) -> None:
        self.domain = domain
        self.protocol = protocol
        self.api_key = api_key
        self.request_timeout = request_timeout
        self._sdk: Any = None
        self._config: Any = None
        self._manager: Any = None

    # -- server -----------------------------------------------------------

    def ping(self) -> None:
        """Readiness probe: the server's own /health endpoint."""

        import httpx

        base = f"{self.protocol}://{self.domain}"
        with httpx.Client(timeout=self.request_timeout, trust_env=False) as http:
            response = http.get(f"{base}/health")
            if response.status_code != 200:
                raise SandboxRuntimeError(
                    f"OpenSandbox server at {self.domain} answered "
                    f"{response.status_code} on /health"
                )

    def close(self) -> None:
        self._close_manager()
        self._config = None
        self._sdk = None

    # -- sandboxes --------------------------------------------------------

    def create(
        self,
        *,
        image: str,
        timeout_seconds: float,
        ready_timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        metadata: Mapping[str, str] | None = None,
        mounts: Sequence[MountEntry] = (),
        resource: Mapping[str, str] | None = None,
    ) -> OsbHandle:
        sdk = self._import_sdk()
        volumes = [_volume(sdk, entry, index) for index, entry in enumerate(mounts)]
        kwargs: dict[str, Any] = {
            "timeout": timedelta(seconds=float(timeout_seconds)),
            "ready_timeout": timedelta(seconds=float(ready_timeout_seconds)),
            "connection_config": self._connection(),
        }
        if env:
            kwargs["env"] = dict(env)
        if metadata:
            kwargs["metadata"] = dict(metadata)
        if volumes:
            kwargs["volumes"] = volumes
        if resource:
            kwargs["resource"] = dict(resource)
        try:
            sandbox = sdk.SandboxSync.create(image, **kwargs)
        except Exception as exc:
            raise _map_create_error(exc, image) from exc
        return SdkOsbHandle(sandbox)

    def connect(self, sandbox_id: str) -> OsbHandle:
        sdk = self._import_sdk()
        try:
            sandbox = sdk.SandboxSync.connect(
                sandbox_id, connection_config=self._connection()
            )
        except Exception as exc:
            raise _map_error(exc, f"connect to sandbox {sandbox_id}") from exc
        return SdkOsbHandle(sandbox)

    def list_by_metadata(
        self, metadata: Mapping[str, str], *, states: Sequence[str] = ("RUNNING",)
    ) -> list[SandboxSummary]:
        manager = self._manager_client()
        try:
            paged = manager.list_sandbox_infos(
                _sandbox_filter(metadata, states, _PAGE_SIZE)
            )
        except Exception as exc:
            raise _map_error(exc, "list sandboxes") from exc
        rows = getattr(paged, "sandbox_infos", None) or []
        return [
            SandboxSummary(
                sandbox_id=str(getattr(row, "id", "") or ""),
                state=str(getattr(getattr(row, "status", None), "state", "") or ""),
                metadata=dict(getattr(row, "metadata", None) or {}),
                created_at=getattr(row, "created_at", None),
                expires_at=getattr(row, "expires_at", None),
            )
            for row in rows
        ]

    def kill(self, sandbox_id: str) -> None:
        manager = self._manager_client()
        try:
            manager.kill_sandbox(sandbox_id)
        except Exception as exc:
            raise _map_error(exc, f"kill sandbox {sandbox_id}") from exc

    # -- internals --------------------------------------------------------

    def _import_sdk(self) -> Any:
        if self._sdk is None:
            import opensandbox

            self._sdk = opensandbox
        return self._sdk

    def _connection(self) -> Any:
        if self._config is None:
            sdk = self._import_sdk()
            from opensandbox.config import ConnectionConfigSync

            del sdk
            self._config = ConnectionConfigSync(
                domain=self.domain,
                protocol=self.protocol,
                api_key=self.api_key or None,
                request_timeout=timedelta(seconds=float(self.request_timeout)),
            )
        return self._config

    def _manager_client(self) -> Any:
        if self._manager is None:
            sdk = self._import_sdk()
            self._manager = sdk.SandboxManagerSync.create(
                connection_config=self._connection()
            )
        return self._manager

    def _close_manager(self) -> None:
        manager = self._manager
        self._manager = None
        if manager is None:
            return
        try:
            manager.close()
        except Exception:  # noqa: BLE001 - best effort
            logger.debug("closing the OpenSandbox manager failed", exc_info=True)


# -- small adapters (imported lazily inside, to keep the module SDK-free) ----


def _run_opts(kwargs: Mapping[str, Any]) -> Any:
    from opensandbox.models.execd import RunCommandOpts

    return RunCommandOpts(**dict(kwargs))


def _directory_entry(path: str, depth: int) -> Any:
    from opensandbox.models.filesystem import DirectoryListEntry

    return DirectoryListEntry(path=path, depth=depth)


def _search_entry(path: str, pattern: str) -> Any:
    from opensandbox.models.filesystem import SearchEntry

    return SearchEntry(path=path, pattern=pattern)


def _write_entry(path: str) -> Any:
    from opensandbox.models.filesystem import WriteEntry

    return WriteEntry(path=path)


def _sandbox_filter(
    metadata: Mapping[str, str], states: Sequence[str], page_size: int
) -> Any:
    from opensandbox.models.sandboxes import SandboxFilter

    return SandboxFilter(
        states=list(states), metadata=dict(metadata), page_size=page_size
    )


def _volume(sdk: Any, entry: MountEntry, index: int) -> Any:
    """One host bind mount. The volume name is required by the SDK and must be
    unique per sandbox, so it is derived from the position in the mount list."""

    from opensandbox.models.sandboxes import Host, Volume

    del sdk
    return Volume(
        name=f"myflow-{index}",
        host=Host(path=entry.host_path),
        mount_path=entry.container_path,
        read_only=entry.read_only,
    )


def _to_entry(row: Any) -> Entry:
    path = str(getattr(row, "path", "") or "")
    raw_type = str(getattr(row, "entry_type", "") or "")
    return Entry(
        name=posixpath.basename(path.rstrip("/")) or path,
        path=path,
        is_dir="dir" in raw_type.lower(),
        size=getattr(row, "size", None),
    )


def _same_path(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


def _join_messages(messages: Any) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for message in messages:
        text = getattr(message, "text", None)
        if text is None:
            text = str(message)
        parts.append(str(text))
    return "".join(parts)


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


def _type_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _status_code(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        error = getattr(exc, "error", None)
        raw = getattr(error, "status_code", None)
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _is_connection_error(exc: BaseException) -> bool:
    names = _type_names(exc)
    return bool(names & {"SandboxConnectionException", "ConnectError", "ConnectionError"})


def _is_timeout_error(exc: BaseException) -> bool:
    names = _type_names(exc)
    return bool(names & {"SandboxTimeoutException", "SandboxReadyTimeoutException", "TimeoutException"})


def _is_not_found(exc: BaseException) -> bool:
    if _status_code(exc) == 404:
        return True
    code = str(getattr(getattr(exc, "error", None), "code", "") or "")
    text = str(exc).lower()
    return "not_found" in code.lower() or "not found" in text


def _map_error(exc: BaseException, action: str) -> Exception:
    if isinstance(exc, (SandboxRuntimeError, SandboxTimeoutError)):
        return exc
    if _is_connection_error(exc):
        return OsbTransportError(f"could not {action}: the sandbox server is unreachable: {exc}")
    if _is_timeout_error(exc):
        return SandboxTimeoutError(f"could not {action}: the sandbox server timed out")
    if _is_not_found(exc):
        return SandboxNotFoundError(f"could not {action}: the sandbox no longer exists")
    return SandboxRuntimeError(f"could not {action}: {exc}")


def _map_command_error(exc: BaseException, timeout: float) -> Exception:
    if _is_timeout_error(exc):
        return SandboxTimeoutError(f"Command timed out after {timeout:g}s")
    return _map_error(exc, "run a command")


def _map_create_error(exc: BaseException, image: str) -> Exception:
    mapped = _map_error(exc, f"create a sandbox from {image}")
    if isinstance(mapped, SandboxRuntimeError) and "unreachable" in str(mapped):
        return mapped
    return mapped


def _map_file_error(exc: BaseException, path: str, *, action: str) -> Exception:
    if isinstance(exc, (SandboxRuntimeError, OSError)):
        return exc
    if _is_connection_error(exc):
        return OsbTransportError(f"could not {action} {path}: the sandbox server is unreachable: {exc}")
    if _is_timeout_error(exc):
        return SandboxTimeoutError(f"the sandbox file API timed out while trying to {action} {path}")
    if _is_not_found(exc):
        return FileNotFoundError(f"{path}: no such file or directory")
    return OSError(f"Failed to {action} {path}: {exc}")


__all__ = [
    "CommandOutput",
    "Entry",
    "GrepResult",
    "OsbClient",
    "OsbHandle",
    "OsbTransportError",
    "SandboxSummary",
    "SdkOsbClient",
    "SdkOsbHandle",
]
