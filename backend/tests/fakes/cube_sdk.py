"""In-memory CubeSandbox SDK fake shared by Cube adapter tests.

The fake mirrors the narrow SDK surface used by CubeSandbox and
CubeSandboxProvider. A root map can bind a container mount (for example
/mnt/user-data) to a real host directory, which lets provider canary tests
exercise actual host reads and writes without a Cube cluster.
"""

from __future__ import annotations

import posixpath
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

FILE_TYPE_FILE = "FILE_TYPE_FILE"
FILE_TYPE_DIRECTORY = "FILE_TYPE_DIRECTORY"


class FilesystemNotFoundError(OSError):
    """Stand-in for cubesandbox.FilesystemNotFoundError."""

    def __init__(self, message: str, status_code: int | None = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FakeCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeFiles:
    """A small file API over an optional host root plus in-memory paths."""

    def __init__(self, root_map: dict[str, Path] | None = None) -> None:
        roots = root_map or {}
        self.root_map = {
            self._normalize(container): Path(host).resolve()
            for container, host in roots.items()
        }
        self._mounts = sorted(
            self.root_map.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._virtual_files: dict[str, bytes] = {}
        self._virtual_dirs: set[str] = {"/"}
        self.writes: list[str] = []
        self.fail_canary = False

    def read(
        self,
        path: str,
        *,
        format: str = "text",
        user: str | None = None,
        **kwargs: Any,
    ) -> str | bytes:
        del user, kwargs
        normalized = self._normalize(path)
        if self.fail_canary and self._under_user_data(normalized):
            data = b"__myflow_canary_failure__"
        else:
            data = self._read_bytes(normalized)
        if format == "bytes":
            return data
        if format in ("text", None):
            return data.decode("utf-8")
        raise ValueError(f"unsupported read format: {format!r}")

    def write(
        self,
        path: str,
        data: str | bytes,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> None:
        del user, kwargs
        normalized = self._normalize(path)
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.writes.append(normalized)
        host = self._host_path(normalized)
        if host is not None:
            host.parent.mkdir(parents=True, exist_ok=True)
            host.write_bytes(payload)
            return

        self._virtual_files[normalized] = payload
        self._virtual_dirs.discard(normalized)
        parent = PurePosixPath(normalized).parent
        while str(parent) != "/":
            self._virtual_dirs.add(str(parent))
            parent = parent.parent

    def write_files(
        self,
        files: list[tuple[str, str | bytes]],
        *,
        user: str | None = None,
    ) -> int:
        for path, data in files:
            self.write(path, data, user=user)
        return len(files)

    def read_bytes(self, path: str) -> bytes:
        return self._read_bytes(self._normalize(path))

    def list(
        self,
        path: str,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del user, kwargs
        normalized = self._normalize(path)
        host = self._host_path(normalized)
        if host is not None:
            if not host.exists():
                raise FilesystemNotFoundError(f"no such file or directory: {path}")
            if not host.is_dir():
                raise NotADirectoryError(path)
            children = sorted(host.iterdir(), key=lambda child: child.name.casefold())
            return [self._host_entry(child) for child in children]

        if not self._virtual_is_dir(normalized):
            if self._virtual_exists(normalized):
                raise NotADirectoryError(path)
            raise FilesystemNotFoundError(f"no such file or directory: {path}")
        return self._virtual_entries(normalized)

    def stat(
        self,
        path: str,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del user, kwargs
        normalized = self._normalize(path)
        host = self._host_path(normalized)
        if host is not None:
            if not host.exists():
                raise FilesystemNotFoundError(f"no such file or directory: {path}")
            return self._host_entry(host)

        if normalized in self._virtual_files:
            return {
                "name": PurePosixPath(normalized).name,
                "type": FILE_TYPE_FILE,
                "size": str(len(self._virtual_files[normalized])),
            }
        if self._virtual_is_dir(normalized):
            return {
                "name": PurePosixPath(normalized).name or "/",
                "type": FILE_TYPE_DIRECTORY,
                "size": "0",
            }
        raise FilesystemNotFoundError(f"no such file or directory: {path}")

    def exists(
        self,
        path: str,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> bool:
        try:
            self.stat(path, user=user, **kwargs)
        except FilesystemNotFoundError:
            return False
        return True

    def make_dir(
        self,
        path: str,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del user, kwargs
        normalized = self._normalize(path)
        host = self._host_path(normalized)
        if host is not None:
            host.mkdir(parents=True, exist_ok=True)
            return self._host_entry(host)

        if normalized in self._virtual_files:
            raise FileExistsError(path)
        self._virtual_dirs.add(normalized)
        parent = PurePosixPath(normalized).parent
        while str(parent) != "/":
            self._virtual_dirs.add(str(parent))
            parent = parent.parent
        return self.stat(normalized)

    def remove(
        self,
        path: str,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> None:
        del user, kwargs
        normalized = self._normalize(path)
        host = self._host_path(normalized)
        if host is not None:
            if not host.exists():
                raise FilesystemNotFoundError(f"no such file or directory: {path}")
            if host.is_dir():
                shutil.rmtree(host)
            else:
                host.unlink()
            return

        if normalized in self._virtual_files:
            del self._virtual_files[normalized]
            return
        if self._virtual_is_dir(normalized):
            prefix = self._prefix(normalized)
            for key in list(self._virtual_files):
                if key.startswith(prefix):
                    del self._virtual_files[key]
            for key in list(self._virtual_dirs):
                if key == normalized or key.startswith(prefix):
                    self._virtual_dirs.discard(key)
            return
        raise FilesystemNotFoundError(f"no such file or directory: {path}")

    def rename(
        self,
        old_path: str,
        new_path: str,
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        old = self._normalize(old_path)
        new = self._normalize(new_path)
        old_host = self._host_path(old)
        new_host = self._host_path(new)
        if old_host is not None and new_host is not None:
            if not old_host.exists():
                raise FilesystemNotFoundError(f"no such file or directory: {old_path}")
            new_host.parent.mkdir(parents=True, exist_ok=True)
            old_host.rename(new_host)
            return self._host_entry(new_host)
        return self._rename_virtual(old, new)

    def _rename_virtual(self, old: str, new: str) -> dict[str, Any]:
        if old not in self._virtual_files:
            raise FilesystemNotFoundError(f"no such file or directory: {old}")
        data = self._virtual_files.pop(old)
        self.write(new, data)
        return self.stat(new)

    def _read_bytes(self, path: str) -> bytes:
        host = self._host_path(path)
        if host is not None:
            try:
                return host.read_bytes()
            except FileNotFoundError as exc:
                raise FilesystemNotFoundError(
                    f"no such file or directory: {path}"
                ) from exc
            except IsADirectoryError as exc:
                raise NotADirectoryError(path) from exc
        if path in self._virtual_files:
            return self._virtual_files[path]
        raise FilesystemNotFoundError(f"no such file or directory: {path}")

    def _host_entry(self, path: Path) -> dict[str, Any]:
        try:
            is_dir = path.is_dir()
            size = 0 if is_dir else path.stat().st_size
        except OSError as exc:
            raise FilesystemNotFoundError(f"no such file or directory: {path}") from exc
        return {
            "name": path.name or "/",
            "type": FILE_TYPE_DIRECTORY if is_dir else FILE_TYPE_FILE,
            "size": str(size),
        }

    def _virtual_entries(self, path: str) -> list[dict[str, Any]]:
        prefix = self._prefix(path)
        names: dict[str, bool] = {}
        for file_path in self._virtual_files:
            if file_path.startswith(prefix):
                name = file_path[len(prefix):].split("/", 1)[0]
                names[name] = "/" in file_path[len(prefix):]
        for dir_path in self._virtual_dirs:
            if dir_path != path and dir_path.startswith(prefix):
                name = dir_path[len(prefix):].split("/", 1)[0]
                names[name] = True

        entries: list[dict[str, Any]] = []
        for name in sorted(names, key=str.casefold):
            full_path = f"{prefix}{name}"
            is_dir = names[name] or self._virtual_is_dir(full_path)
            size = 0 if is_dir else len(self._virtual_files.get(full_path, b""))
            entries.append(
                {
                    "name": name,
                    "type": FILE_TYPE_DIRECTORY if is_dir else FILE_TYPE_FILE,
                    "size": str(size),
                }
            )
        return entries

    def _virtual_exists(self, path: str) -> bool:
        return path in self._virtual_files or self._virtual_is_dir(path)

    def _virtual_is_dir(self, path: str) -> bool:
        if path in self._virtual_dirs:
            return True
        prefix = self._prefix(path)
        return any(key.startswith(prefix) for key in self._virtual_files) or any(
            key.startswith(prefix) for key in self._virtual_dirs if key != path
        )

    def _host_path(self, path: str) -> Path | None:
        for container, root in self._mounts:
            if path == container or path.startswith(f"{container}/"):
                relative = path[len(container):].lstrip("/")
                parts = PurePosixPath(relative).parts if relative else ()
                candidate = root.joinpath(*parts).resolve()
                if candidate != root and not candidate.is_relative_to(root):
                    raise PermissionError(f"path escapes fake mount root: {path}")
                return candidate
        return None

    @staticmethod
    def _prefix(path: str) -> str:
        return "/" if path == "/" else f"{path.rstrip('/')}/"

    @staticmethod
    def _under_user_data(path: str) -> bool:
        return path == "/mnt/user-data" or path.startswith("/mnt/user-data/")

    @staticmethod
    def _normalize(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"path must be absolute: {path!r}")
        normalized = posixpath.normpath(path)
        if not normalized.startswith("/"):
            raise ValueError(f"path must be absolute: {path!r}")
        return normalized


class FakeCommands:
    """Record command calls and optionally delegate to a handler."""

    def __init__(
        self,
        handler: Callable[..., FakeCommandResult] | None = None,
    ) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        cmd: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        **kwargs: Any,
    ) -> FakeCommandResult:
        call = {
            "cmd": cmd,
            "timeout": timeout,
            "cwd": cwd,
            "envs": envs,
            "user": user,
            **kwargs,
        }
        self.calls.append(call)
        if self.handler is None:
            return FakeCommandResult()
        return self.handler(
            cmd,
            timeout=timeout,
            cwd=cwd,
            envs=envs,
            user=user,
            **kwargs,
        )


class FakeSdkSandbox:
    """Fake per-sandbox SDK handle."""

    def __init__(
        self,
        sandbox_id: str = "s1",
        *,
        files: FakeFiles | None = None,
        commands: FakeCommands | None = None,
        metadata: dict[str, str] | None = None,
        factory: "FakeSdkFactory | None" = None,
        template: str | None = None,
        timeout: int | None = None,
        lifecycle: dict | None = None,
        network: dict | None = None,
        allow_internet_access: bool = True,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.files = files if files is not None else FakeFiles()
        self.commands = commands if commands is not None else FakeCommands()
        self.metadata = dict(metadata or {})
        self.template = template
        self.timeout = timeout
        self.lifecycle = dict(lifecycle or {})
        self.network = dict(network or {})
        self.allow_internet_access = allow_internet_access
        self.killed = False
        self.paused = False
        self.closed = False
        self.kill_calls = 0
        self.pause_calls = 0
        self.close_calls = 0
        self._factory = factory

    def kill(self) -> None:
        self.killed = True
        self.kill_calls += 1
        if self._factory is not None:
            self._factory._record_kill(self)

    def pause(self) -> None:
        self.paused = True
        self.pause_calls += 1

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class FakeSdkFactory:
    """Fake control plane used by provider lifecycle tests."""

    def __init__(self, root_map: dict[str, Path] | None = None) -> None:
        self.root_map = dict(root_map or {})
        self.created: list[FakeSdkSandbox] = []
        self.connected: list[str] = []
        self.connected_sandboxes: list[FakeSdkSandbox] = []
        self.existing: list[dict] = []
        self.calls: list[dict] = []
        self.health_calls = 0
        self._next_id = 1
        self._fail_canary = False

    @property
    def fail_canary(self) -> bool:
        return self._fail_canary

    @fail_canary.setter
    def fail_canary(self, value: bool) -> None:
        self._fail_canary = bool(value)
        for sandbox in [*self.created, *self.connected_sandboxes]:
            sandbox.files.fail_canary = self._fail_canary

    def create(
        self,
        *,
        template: str,
        timeout: int,
        metadata: dict[str, str],
        lifecycle: dict,
        network: dict,
        allow_internet_access: bool,
    ) -> FakeSdkSandbox:
        sandbox_id = f"s{self._next_id}"
        self._next_id += 1
        self.calls.append({"op": "create", "metadata": dict(metadata)})
        sandbox = FakeSdkSandbox(
            sandbox_id,
            files=FakeFiles(self.root_map),
            metadata=metadata,
            factory=self,
            template=template,
            timeout=timeout,
            lifecycle=lifecycle,
            network=network,
            allow_internet_access=allow_internet_access,
        )
        sandbox.files.fail_canary = self._fail_canary
        self.created.append(sandbox)
        return sandbox

    def connect(self, sandbox_id: str) -> FakeSdkSandbox:
        self.calls.append({"op": "connect", "sandbox_id": sandbox_id})
        row = next(
            (
                item
                for item in self.existing
                if item.get("sandboxID") == sandbox_id
            ),
            None,
        )
        metadata = dict(row.get("metadata", {})) if row is not None else {}
        sandbox = FakeSdkSandbox(
            sandbox_id,
            files=FakeFiles(self.root_map),
            metadata=metadata,
            factory=self,
        )
        sandbox.files.fail_canary = self._fail_canary
        self.connected.append(sandbox_id)
        self.connected_sandboxes.append(sandbox)
        return sandbox

    def list_by_metadata(self, metadata: Any) -> list[dict]:
        query = dict(metadata)
        self.calls.append({"op": "list_by_metadata", "metadata": query})
        return [
            row
            for row in self.existing
            if not row.get("killed")
            and row.get("state") not in ("killed", "terminated")
            and all(
                str((row.get("metadata") or {}).get(key)) == str(value)
                for key, value in query.items()
            )
        ]

    def health(self) -> None:
        self.calls.append({"op": "health"})
        self.health_calls += 1

    def _record_kill(self, sandbox: FakeSdkSandbox) -> None:
        for row in self.existing:
            if row.get("sandboxID") == sandbox.sandbox_id:
                row["killed"] = True
                return


__all__ = [
    "FILE_TYPE_DIRECTORY",
    "FILE_TYPE_FILE",
    "FakeCommandResult",
    "FakeCommands",
    "FakeFiles",
    "FakeSdkFactory",
    "FakeSdkSandbox",
    "FilesystemNotFoundError",
]
