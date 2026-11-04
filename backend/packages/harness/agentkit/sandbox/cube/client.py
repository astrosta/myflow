"""E2B protocol boundary for CubeSandbox.

The official E2B Python SDK is optional. It is imported only inside the methods
that need it, so a local-only deployment can import the sandbox package without
installing agentkit[cube].

The Protocols here are the narrow surface CubeSandboxProvider and CubeSandbox
depend on. The adapters below translate E2B objects and exceptions into that
surface; the fake in tests/fakes/cube_sdk.py implements the same surface.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SdkSandbox(Protocol):
    """The subset of an E2B Sandbox used by this project."""

    sandbox_id: str
    commands: Any
    files: Any

    def kill(self) -> None: ...

    def pause(self) -> None: ...

    def close(self) -> None: ...


class SdkFactory(Protocol):
    """The E2B operations the provider needs."""

    def create(
        self,
        *,
        template: str,
        timeout: int,
        metadata: dict[str, str],
        lifecycle: dict,
        network: dict,
        allow_internet_access: bool,
    ) -> SdkSandbox: ...

    def connect(self, sandbox_id: str) -> SdkSandbox: ...

    def list_by_metadata(self, metadata: Mapping[str, str]) -> list[dict]: ...

    def health(self) -> None: ...


_GONE_STATUS_CODES = frozenset({404, 410})

# E2B reports the sandbox's own lifecycle in the exception type rather than in
# an HTTP status: the control plane answers `SandboxNotFoundException` and the
# proxy in front of envd answers `SandboxNotRunningException` when the MicroVM
# was killed, paused away, or timed out. Neither carries a status code, so the
# provider's re-acquire decision has to read the type.
_GONE_TYPE_NAMES = frozenset({"SandboxNotFoundException", "SandboxNotRunningException"})
# A missing *path* inside a live sandbox is also a not-found, but it must never
# be mistaken for a dead sandbox: the handle is still usable.
_MISSING_PATH_TYPE_NAMES = frozenset(
    {"FileNotFoundException", "VolumePathNotFoundException"}
)
# A sandbox that could not be reached is not a timeout of the request itself,
# even though E2B makes these subclasses of its `TimeoutException`.
_UNREACHABLE_TYPE_NAMES = frozenset(
    {"SandboxUnreachableException", "SandboxNotRunningException"}
)


def error_status(exc: BaseException) -> int | None:
    """Return an SDK/HTTP status code attached to an exception, if any."""

    status = getattr(exc, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _type_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def is_gone_error(exc: BaseException) -> bool:
    """Whether ``exc`` may mean the remote sandbox itself no longer exists.

    The owner must stop handing the handle out (the runtime then re-acquires),
    so anything that names a missing path answers ``False`` here even though
    both cases are a not-found on the wire. A bare 404/410 is ambiguous and
    stays ``True``: dropping the handle costs one re-acquire, while serving a
    dead sandbox costs the whole run.
    """

    names = _type_names(exc)
    if names & _MISSING_PATH_TYPE_NAMES:
        return False
    if names & _GONE_TYPE_NAMES:
        return True
    return error_status(exc) in _GONE_STATUS_CODES


def is_missing_path_error(exc: BaseException) -> bool:
    """Whether ``exc`` means a path inside the sandbox does not exist.

    Callers that asked about a path must check this before ``is_gone_error``:
    a bare 404 answers both, and for a path request the missing path is the
    reading that keeps the tool error useful.
    """

    names = _type_names(exc)
    if names & _MISSING_PATH_TYPE_NAMES:
        return True
    if names & _GONE_TYPE_NAMES:
        return False
    return error_status(exc) in _GONE_STATUS_CODES


def is_unreachable_error(exc: BaseException) -> bool:
    """Whether ``exc`` says the sandbox could not be reached at all."""

    return bool(_type_names(exc) & _UNREACHABLE_TYPE_NAMES)


class SandboxGoneError(OSError):
    """The remote sandbox no longer exists: killed, paused away, or timed out.

    Subclasses OSError so the Sandbox file contract is unchanged, while the
    provider still learns that this handle must not be served again.
    """


@dataclass(frozen=True)
class _CommandResult:
    stdout: str
    stderr: str
    exit_code: int


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class _CommandsAdapter:
    """Convert E2B's CommandExitException into the project's result shape.

    E2B raises where the rest of this project expects a result with a non-zero
    exit code, and the tool layer renders "exit code N" itself. A command that
    exits 1 is therefore normal tool output, not an SDK failure.
    """

    def __init__(self, sdk: Any, commands: Any) -> None:
        exit_exception = getattr(sdk, "CommandExitException", None)
        self._exit_exception: type[BaseException] | None = (
            exit_exception if isinstance(exit_exception, type) else None
        )
        self._commands = commands

    def run(
        self,
        cmd: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return self._commands.run(
                cmd,
                timeout=timeout,
                cwd=cwd,
                envs=envs,
                user=user,
                **kwargs,
            )
        except Exception as exc:
            if self._exit_exception is None or not isinstance(
                exc, self._exit_exception
            ):
                raise
            return _CommandResult(
                stdout=_as_text(getattr(exc, "stdout", "")),
                stderr=_as_text(getattr(exc, "stderr", "")),
                exit_code=int(getattr(exc, "exit_code", -1)),
            )


class _FilesAdapter:
    """Normalize E2B EntryInfo objects and bytes results for existing helpers."""

    def __init__(self, sdk: Any, files: Any) -> None:
        self._sdk = sdk
        self._files = files

    def read(self, path: str, *, format: str = "text", user: str | None = None) -> Any:
        data = self._files.read(path, format=format, user=user)
        if format == "bytes":
            return bytes(data)
        return data

    def write(self, path: str, data: str | bytes, *, user: str | None = None) -> Any:
        return self._files.write(path, data, user=user)

    def list(self, path: str, *, user: str | None = None) -> list[dict[str, Any]]:
        entries = self._files.list(path, depth=1, user=user)
        return [self._entry(entry) for entry in entries]

    def stat(self, path: str, *, user: str | None = None) -> dict[str, Any]:
        entry = self._files.get_info(path, user=user)
        return self._entry(entry)

    def exists(self, path: str, *, user: str | None = None) -> bool:
        return bool(self._files.exists(path, user=user))

    def make_dir(self, path: str, *, user: str | None = None) -> Any:
        return self._files.make_dir(path, user=user)

    @staticmethod
    def _entry(entry: Any) -> dict[str, Any]:
        type_value = getattr(entry, "type", None)
        normalized = str(getattr(type_value, "value", type_value) or "").lower()
        return {
            "name": str(getattr(entry, "name", "") or ""),
            "type": "FILE_TYPE_DIRECTORY" if normalized == "dir" else "FILE_TYPE_FILE",
            "size": str(getattr(entry, "size", 0) or 0),
            "path": str(getattr(entry, "path", "") or ""),
        }


class _SandboxAdapter:
    """The SdkSandbox Protocol over an E2B Sandbox instance."""

    def __init__(self, sdk: Any, sandbox: Any) -> None:
        self._sandbox = sandbox
        self.sandbox_id = sandbox.sandbox_id
        self.commands = _CommandsAdapter(sdk, sandbox.commands)
        self.files = _FilesAdapter(sdk, sandbox.files)

    def kill(self) -> None:
        self._sandbox.kill()

    def pause(self) -> None:
        self._sandbox.pause()

    def close(self) -> None:
        # E2B's sync Sandbox has no close(); its transport is released with the
        # handle. Keep the method so the provider's lifecycle contract is stable.
        return None


class E2BFactory:
    """Thin adapter around the official E2B Python SDK."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        template_id: str,
        sandbox_domain: str,
        request_timeout: float,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.template_id = template_id
        self.sandbox_domain = sandbox_domain
        self.request_timeout = request_timeout

    def create(
        self,
        *,
        template: str,
        timeout: int,
        metadata: dict[str, str],
        lifecycle: dict,
        network: dict,
        allow_internet_access: bool,
    ) -> SdkSandbox:
        sdk = self._import_sdk()
        sandbox = sdk.Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=metadata,
            lifecycle=lifecycle,
            network=network,
            allow_internet_access=allow_internet_access,
            **self._connection_kwargs(),
        )
        return _SandboxAdapter(sdk, sandbox)

    def connect(self, sandbox_id: str) -> SdkSandbox:
        sdk = self._import_sdk()
        sandbox = sdk.Sandbox.connect(
            sandbox_id,
            **self._connection_kwargs(),
        )
        return _SandboxAdapter(sdk, sandbox)

    def list_by_metadata(self, metadata: Mapping[str, str]) -> list[dict]:
        sdk = self._import_sdk()
        query = sdk.SandboxQuery(
            metadata=dict(metadata),
            state=[sdk.SandboxState.RUNNING, sdk.SandboxState.PAUSED],
        )
        paginator = sdk.Sandbox.list(query, **self._connection_kwargs())
        rows: list[dict] = []
        while paginator.has_next:
            for info in paginator.next_items():
                rows.append(
                    {
                        "sandboxID": info.sandbox_id,
                        "metadata": dict(info.metadata or {}),
                        "state": str(getattr(info.state, "value", info.state)),
                        "startedAt": (
                            info.started_at.isoformat()
                            if getattr(info, "started_at", None) is not None
                            else None
                        ),
                    }
                )
        return rows

    def health(self) -> None:
        sdk = self._import_sdk()
        paginator = sdk.Sandbox.list(limit=1, **self._connection_kwargs())
        if paginator.has_next:
            paginator.next_items()

    def _connection_kwargs(self) -> dict[str, Any]:
        return {
            "api_url": self.api_url,
            "api_key": self.api_key or None,
            "domain": self.sandbox_domain,
            "request_timeout": self.request_timeout,
        }

    @staticmethod
    def _import_sdk() -> Any:
        import e2b

        return e2b


__all__ = [
    "E2BFactory",
    "SandboxGoneError",
    "SdkFactory",
    "SdkSandbox",
    "error_status",
    "is_gone_error",
    "is_missing_path_error",
    "is_unreachable_error",
]
