"""Fake AioClient: the data-plane protocol without HTTP.

Used by the Sandbox contract tests and the provider lifecycle tests. Every call
is recorded, and a test can inject the failure it cares about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentkit.sandbox.aio_sandbox.client import (
    AioTransportError,
    CommandOutput,
    Entry,
    GrepResult,
)


@dataclass
class FakeAioClient:
    ping_error: BaseException | None = None
    command_output: CommandOutput = field(default_factory=lambda: CommandOutput("", 0))
    command_error: BaseException | None = None
    text: str = ""
    text_error: BaseException | None = None
    byte_content: bytes = b""
    bytes_error: BaseException | None = None
    entries: list[Entry] = field(default_factory=list)
    list_error: BaseException | None = None
    glob_result: tuple[list[str], bool] = field(default_factory=lambda: ([], False))
    glob_error: BaseException | None = None
    grep_result: tuple[list[GrepResult], bool] = field(default_factory=lambda: ([], False))
    grep_error: BaseException | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    byte_writes: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    pings: int = 0
    closed: bool = False

    def ping(self) -> None:
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error

    def close(self) -> None:
        self.closed = True

    def command(
        self,
        command: str,
        *,
        env: Any = None,
        timeout: float,
        max_output_bytes: int,
    ) -> CommandOutput:
        self.commands.append(
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

    def read_text(self, path: str, *, start_line=None, end_line=None) -> str:
        if self.text_error is not None:
            raise self.text_error
        return self.text

    def write_text(self, path: str, content: str, *, append: bool = False) -> None:
        self.writes.append({"path": path, "content": content, "append": append})

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        if self.bytes_error is not None:
            raise self.bytes_error
        return self.byte_content

    def write_bytes(self, path: str, data: bytes) -> None:
        self.byte_writes.append({"path": path, "data": data})

    def list_entries(self, path: str, *, max_depth: int) -> list[Entry]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.entries)

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200):
        if self.glob_error is not None:
            raise self.glob_error
        return self.glob_result

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ):
        if self.grep_error is not None:
            raise self.grep_error
        return self.grep_result


__all__ = ["FakeAioClient", "AioTransportError"]
