"""Fake AIO data plane for the sandbox client and provider tests.

It records every SDK call and lets a test inject the failures that matter: a
missing bash namespace (an image too old for /v1/bash), file errors, transport
errors, and the statuses bash.exec can return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ApiError(Exception):
    """Stand-in for agent_sandbox.core.api_error.ApiError."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"API error {status_code}")
        self.status_code = status_code


class FileNotFoundException(Exception):
    """Stand-in for the SDK's file-not-found error."""


class ValidationError(Exception):
    """Stand-in for a pydantic validation error on an error envelope."""


@dataclass
class _Payload:
    data: Any = None


@dataclass
class FakeBashExec:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = 0
    status: str = "completed"


@dataclass
class FakeFileEntry:
    name: str
    path: str
    is_directory: bool = False
    size: int | None = 0


@dataclass
class FakeGrepMatch:
    file: str
    line_number: int
    line_content: str


@dataclass
class SessionRecord:
    session_id: str
    commands: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False


class _Bash:
    def __init__(self, fake: "FakeAioSdk") -> None:
        self._fake = fake

    def create_session(self, *, session_id: str, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("bash.create_session", {"session_id": session_id, **kwargs}))
        if self._fake.bash_unsupported:
            raise ApiError(404, "not found")
        self._fake.sessions[session_id] = SessionRecord(session_id=session_id)
        return _Payload()

    def exec(self, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("bash.exec", dict(kwargs)))
        if self._fake.bash_error is not None:
            raise self._fake.bash_error
        session_id = kwargs.get("session_id")
        record = self._fake.sessions.get(str(session_id))
        if record is not None:
            record.commands.append(dict(kwargs))
        result = self._fake.bash_result
        return _Payload(
            data=FakeBashExec(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                status=result.status,
            )
        )

    def close_session(self, session_id: str, **kwargs: Any) -> _Payload:
        del kwargs
        self._fake.calls.append(("bash.close_session", {"session_id": session_id}))
        record = self._fake.sessions.get(session_id)
        if record is not None:
            record.closed = True
        return _Payload()


class _File:
    def __init__(self, fake: "FakeAioSdk") -> None:
        self._fake = fake

    def read_file(self, *, file: str, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("file.read_file", {"file": file, **kwargs}))
        self._fake.raise_if_error()
        content = self._fake.files.get(file)
        if content is None:
            raise ValidationError("data: {}")
        return _Payload(data=_ReadData(content=content, file=file))

    def write_file(self, *, file: str, content: str, append: Any = None, **kwargs: Any) -> _Payload:
        self._fake.calls.append(
            ("file.write_file", {"file": file, "content": content, "append": append, **kwargs})
        )
        self._fake.raise_if_error()
        if append and file in self._fake.files:
            self._fake.files[file] += content
        else:
            self._fake.files[file] = content
        return _Payload()

    def download_file(self, *, path: str, **kwargs: Any) -> Any:
        self._fake.calls.append(("file.download_file", {"path": path, **kwargs}))
        self._fake.raise_if_error()
        content = self._fake.files.get(path)
        if content is None:
            raise FileNotFoundException(path)
        raw = content.encode("utf-8")
        if self._fake.download_chunks:
            size = self._fake.download_chunks
            return iter([raw[index:index + size] for index in range(0, len(raw), size)])
        return iter([raw])

    def upload_file(self, *, file: Any, path: str | None = None, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("file.upload_file", {"file": file, "path": path, **kwargs}))
        self._fake.raise_if_error()
        self._fake.files[str(path)] = bytes(file).decode("utf-8")
        return _Payload()

    def list_path(self, *, path: str, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("file.list_path", {"path": path, **kwargs}))
        self._fake.raise_if_error()
        entries = self._fake.listings.get(path)
        if entries is None:
            raise FileNotFoundException(path)
        return _Payload(data=_ListData(files=list(entries)))

    def glob_files(self, *, path: str, pattern: str, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("file.glob_files", {"path": path, "pattern": pattern, **kwargs}))
        self._fake.raise_if_error()
        return _Payload(
            data=_GlobData(
                files=list(self._fake.glob_matches),
                truncated=self._fake.glob_truncated,
            )
        )

    def grep_files(self, *, path: str, pattern: str, **kwargs: Any) -> _Payload:
        self._fake.calls.append(("file.grep_files", {"path": path, "pattern": pattern, **kwargs}))
        self._fake.raise_if_error()
        return _Payload(
            data=_GrepData(
                matches=[FakeGrepMatch(**match) for match in self._fake.grep_matches],
                truncated=self._fake.grep_truncated,
            )
        )


@dataclass
class _ReadData:
    content: str
    file: str


@dataclass
class _ListData:
    files: list[FakeFileEntry]


@dataclass
class _GlobData:
    files: list[FakeFileEntry]
    truncated: bool = False


@dataclass
class _GrepData:
    matches: list[FakeGrepMatch]
    truncated: bool = False


class FakeAioSdk:
    """A stand-in for the agent_sandbox module (bash + file clients)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: dict[str, SessionRecord] = {}
        self.files: dict[str, str] = {}
        self.listings: dict[str, list[FakeFileEntry]] = {}
        self.glob_matches: list[FakeFileEntry] = []
        self.glob_truncated = False
        self.grep_matches: list[dict[str, Any]] = []
        self.grep_truncated = False
        self.bash_result = FakeBashExec()
        self.bash_unsupported = False
        self.bash_error: BaseException | None = None
        self.file_error: BaseException | None = None
        self.download_chunks: int = 0
        self.bash = _Bash(self)
        self.file = _File(self)

    def raise_if_error(self) -> None:
        if self.file_error is not None:
            raise self.file_error

    def calls_of(self, name: str) -> list[dict[str, Any]]:
        return [payload for call, payload in self.calls if call == name]


__all__ = [
    "ApiError",
    "FakeAioSdk",
    "FakeBashExec",
    "FakeFileEntry",
    "FileNotFoundException",
    "SessionRecord",
    "ValidationError",
]
