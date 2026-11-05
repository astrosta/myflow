"""Fake OpenSandbox client and handles for the provider/sandbox tests.

With a root map the handle reads and writes real host files through the mount
map, which is what lets the provider canary run for real in a unit test. Without
one it replays canned values, which is what the contract tests want.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agentkit.sandbox.host_mounts import MountEntry
from agentkit.sandbox.opensandbox.client import (
    CommandOutput,
    Entry,
    SandboxSummary,
)
from agentkit.sandbox.exceptions import SandboxNotFoundError


@dataclass
class FakeOsbHandle:
    sandbox_id: str = "sbx-1"
    running: bool = True
    root_map: dict[str, str] | None = None
    command_output: CommandOutput = field(default_factory=lambda: CommandOutput("", 0))
    command_error: BaseException | None = None
    code_output: CommandOutput = field(default_factory=lambda: CommandOutput("", 0))
    code_error: BaseException | None = None
    code_calls: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    text_error: BaseException | None = None
    byte_content: bytes = b""
    bytes_error: BaseException | None = None
    entries: list[Entry] = field(default_factory=list)
    list_error: BaseException | None = None
    search_result: tuple[list[Entry], bool] = field(default_factory=lambda: ([], False))
    search_error: BaseException | None = None
    dirs_created: list[str] = field(default_factory=list)
    command_calls: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    byte_writes: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False
    info_error: BaseException | None = None

    # -- host-mounted file access ---------------------------------------

    def _host(self, path: str) -> Path:
        if self.root_map is None:
            raise AssertionError("handle has no root map")
        normalized = "/" + path.strip("/")
        for container, root in sorted(
            self.root_map.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if normalized == container or normalized.startswith(container + "/"):
                relative = normalized[len(container):].lstrip("/")
                return Path(root) / relative if relative else Path(root)
        raise FileNotFoundError(path)

    # -- OsbHandle -------------------------------------------------------

    def is_running(self) -> bool:
        if self.info_error is not None:
            raise self.info_error
        return self.running

    def run_command(
        self,
        command: str,
        *,
        env: Any = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput:
        self.command_calls.append(
            {
                "command": command,
                "env": env,
                "timeout": timeout,
                "max_output_bytes": max_output_bytes,
            }
        )
        if self.command_error is not None:
            raise self.command_error
        return self.command_output

    def read_text(self, path: str) -> str:
        if self.root_map is not None:
            return self._host(path).read_text(encoding="utf-8")
        if self.text_error is not None:
            raise self.text_error
        return self.text

    def write_text(self, path: str, content: str) -> None:
        self.writes.append({"path": path, "content": content})
        if self.root_map is not None:
            target = self._host(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        if self.root_map is not None:
            data = self._host(path).read_bytes()
        else:
            if self.bytes_error is not None:
                raise self.bytes_error
            data = self.byte_content
        if len(data) > max_bytes:
            raise OSError(f"Failed to read {path}: exceeds the limit")
        return data

    def write_bytes(self, path: str, data: bytes) -> None:
        self.byte_writes.append({"path": path, "data": data})
        if self.root_map is not None:
            target = self._host(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]:
        if self.root_map is not None:
            root = self._host(path)
            if not root.is_dir():
                raise NotADirectoryError(path)
            rows: list[Entry] = []
            prefix = path.rstrip("/")
            for child in sorted(root.iterdir()):
                rows.append(
                    Entry(
                        name=child.name,
                        path=prefix + "/" + child.name,
                        is_dir=child.is_dir(),
                        size=None if child.is_dir() else child.stat().st_size,
                    )
                )
            return rows
        if self.list_error is not None:
            raise self.list_error
        return list(self.entries)

    def search(
        self, path: str, pattern: str, *, max_results: int
    ) -> tuple[list[Entry], bool]:
        if self.search_error is not None:
            raise self.search_error
        return self.search_result

    def create_directories(self, path: str) -> None:
        self.dirs_created.append(path)
        if self.root_map is not None:
            self._host(path).mkdir(parents=True, exist_ok=True)

    def run_code(self, code: str, *, language: str, timeout: float) -> CommandOutput:
        self.code_calls.append(
            {"code": code, "language": language, "timeout": timeout}
        )
        if self.code_error is not None:
            raise self.code_error
        return self.code_output

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeOsbClient:
    ping_error: BaseException | None = None
    ping_calls: int = 0
    create_error: BaseException | None = None
    connect_error: BaseException | None = None
    list_error: BaseException | None = None
    kill_error: BaseException | None = None
    existing: list[SandboxSummary] = field(default_factory=list)
    created: list[FakeOsbHandle] = field(default_factory=list)
    connects: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False
    running_state: str = "RUNNING"
    default_root_map: dict[str, str] | None = None
    _next: int = 1
    _last_root_map: dict[str, str] = field(default_factory=dict)

    def ping(self) -> None:
        self.ping_calls += 1
        if self.ping_error is not None:
            raise self.ping_error

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
        entrypoint: Sequence[str] | None = None,
    ) -> FakeOsbHandle:
        self.create_calls.append(
            {
                "image": image,
                "timeout_seconds": timeout_seconds,
                "ready_timeout_seconds": ready_timeout_seconds,
                "env": dict(env or {}),
                "metadata": dict(metadata or {}),
                "mounts": list(mounts),
                "resource": dict(resource or {}),
                "entrypoint": list(entrypoint) if entrypoint else None,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        root_map = {entry.container_path: entry.host_path for entry in mounts}
        self._last_root_map = root_map
        handle = FakeOsbHandle(sandbox_id=f"sbx-{self._next}", root_map=root_map)
        self._next += 1
        self.created.append(handle)
        return handle

    def connect(self, sandbox_id: str) -> FakeOsbHandle:
        self.connects.append(sandbox_id)
        if self.connect_error is not None:
            raise self.connect_error
        for handle in self.created:
            if handle.sandbox_id == sandbox_id:
                return handle
        for row in self.existing:
            if row.sandbox_id == sandbox_id:
                # An adopted sandbox has the same mounts the create path used:
                # the provider only adopts when the fingerprint matches.
                root_map = self.default_root_map or self._last_root_map
                handle = FakeOsbHandle(sandbox_id=sandbox_id, root_map=root_map)
                self.created.append(handle)
                return handle
        raise SandboxNotFoundError(f"sandbox {sandbox_id} not found")

    def list_by_metadata(
        self, metadata: Mapping[str, str], *, states: Sequence[str] = ("RUNNING",)
    ) -> list[SandboxSummary]:
        self.list_calls.append({"metadata": dict(metadata), "states": list(states)})
        if self.list_error is not None:
            raise self.list_error
        return [
            row
            for row in self.existing
            if all(row.metadata.get(key) == value for key, value in metadata.items())
        ]

    def kill(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)
        if self.kill_error is not None:
            raise self.kill_error
        self.existing = [row for row in self.existing if row.sandbox_id != sandbox_id]
        self.created = [h for h in self.created if h.sandbox_id != sandbox_id]

    def close(self) -> None:
        self.closed = True

    # -- helpers ----------------------------------------------------------

    def summary(self, sandbox_id: str, metadata: Mapping[str, str], *, state: str = "RUNNING") -> SandboxSummary:
        row = SandboxSummary(
            sandbox_id=sandbox_id,
            state=state,
            metadata=dict(metadata),
            created_at=datetime(2026, 1, 1),
        )
        self.existing.append(row)
        return row


__all__ = ["FakeOsbClient", "FakeOsbHandle"]
