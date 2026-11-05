"""E2B protocol boundary tests.

The client is the only place that knows the official E2B Python SDK. These
tests drive it against a fake `e2b` module so the mapping stays visible: what
the provider asks for, what the adapters hand back, and which E2B exceptions
mean "the sandbox is gone" rather than "this path is missing".
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from agentkit.sandbox.e2b.client import (
    E2BFactory,
    SandboxGoneError,
    error_status,
    is_gone_error,
    is_missing_path_error,
    is_unreachable_error,
)


def _factory(**overrides: object) -> E2BFactory:
    options: dict[str, object] = {
        "api_url": "http://cube:3000/",
        "api_key": "",
        "template_id": "tpl",
        "sandbox_domain": "cube.app",
        "request_timeout": 5,
    }
    options.update(overrides)
    return E2BFactory(**options)  # type: ignore[arg-type]


def _create(factory: E2BFactory):
    return factory.create(
        template="tpl",
        timeout=60,
        metadata={"myflow": "1"},
        lifecycle={"on_timeout": "pause", "auto_resume": True},
        network={"allow_public_traffic": False},
        allow_internet_access=True,
    )


# -- lazy import -----------------------------------------------------------


def test_client_module_does_not_import_e2b_at_import_time() -> None:
    import agentkit.sandbox.e2b.client as client_module

    assert "e2b" not in vars(client_module)


def test_missing_e2b_install_raises_import_error(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "e2b", None)

    with pytest.raises(ImportError):
        _factory().health()


# -- create / connect / list ----------------------------------------------


def test_create_passes_connection_opts_and_sandbox_fields(monkeypatch) -> None:
    _, calls = _install_fake_e2b(monkeypatch)

    sandbox = _create(_factory(api_key="secret"))

    assert sandbox.sandbox_id == "created-1"
    assert calls["create"] == {
        "template": "tpl",
        "timeout": 60,
        "metadata": {"myflow": "1"},
        "lifecycle": {"on_timeout": "pause", "auto_resume": True},
        "network": {"allow_public_traffic": False},
        "allow_internet_access": True,
        "api_url": "http://cube:3000",
        "api_key": "secret",
        "domain": "cube.app",
        "request_timeout": 5,
    }


def test_create_without_api_key_lets_e2b_read_the_environment(monkeypatch) -> None:
    _, calls = _install_fake_e2b(monkeypatch)

    _create(_factory())

    assert calls["create"]["api_key"] is None


def test_adapter_delegates_pause_and_kill_and_tolerates_missing_close(monkeypatch) -> None:
    module, _ = _install_fake_e2b(monkeypatch)
    sandbox = _create(_factory())

    sandbox.pause()
    sandbox.kill()
    # E2B's sync Sandbox has no close(); the provider's lifecycle still calls it.
    sandbox.close()

    raw = module.Sandbox.created[-1]
    assert raw.paused is True
    assert raw.killed is True
    assert not hasattr(raw, "close")


def test_connect_uses_the_class_method_variant(monkeypatch) -> None:
    _, calls = _install_fake_e2b(monkeypatch)

    sandbox = _factory(api_key="secret").connect("remote-1")

    assert sandbox.sandbox_id == "remote-1"
    assert calls["connect"] == {
        "sandbox_id": "remote-1",
        "api_url": "http://cube:3000",
        "api_key": "secret",
        "domain": "cube.app",
        "request_timeout": 5,
    }


def test_list_by_metadata_builds_the_query_and_follows_the_paginator(
    monkeypatch,
) -> None:
    _, calls = _install_fake_e2b(
        monkeypatch,
        pages=[
            [
                {
                    "sandbox_id": "s1",
                    "metadata": {"myflow": "1"},
                    "state": "running",
                    "started_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            ],
            [
                {
                    "sandbox_id": "s2",
                    "metadata": {"myflow": "1"},
                    "state": "paused",
                    "started_at": None,
                }
            ],
        ],
    )

    rows = _factory().list_by_metadata({"myflow": "1", "myflow.user": "u1"})

    assert [row["sandboxID"] for row in rows] == ["s1", "s2"]
    assert rows[0]["metadata"] == {"myflow": "1"}
    assert rows[0]["state"] == "running"
    assert rows[0]["startedAt"] == "2026-01-01T00:00:00+00:00"
    assert rows[1]["startedAt"] is None
    assert calls["query"] == {
        "metadata": {"myflow": "1", "myflow.user": "u1"},
        "state": ["running", "paused"],
    }
    assert calls["list_calls"] == 2


def test_health_lists_one_sandbox(monkeypatch) -> None:
    _, calls = _install_fake_e2b(monkeypatch, pages=[[]])

    assert _factory().health() is None
    assert calls["list_limit"] == 1
    assert calls["list_opts"] == {
        "api_url": "http://cube:3000",
        "api_key": None,
        "domain": "cube.app",
        "request_timeout": 5,
    }


# -- adapters --------------------------------------------------------------


def test_commands_adapter_reports_a_nonzero_exit_as_a_result(monkeypatch) -> None:
    module, _ = _install_fake_e2b(monkeypatch)
    sandbox = _create(_factory())
    commands = module.Sandbox.created[-1].commands
    commands.result = ("partial output", "boom", 2)

    result = sandbox.commands.run("false", timeout=5, user="root")

    assert (result.stdout, result.stderr, result.exit_code) == (
        "partial output",
        "boom",
        2,
    )
    assert commands.calls == [
        {"cmd": "false", "timeout": 5, "cwd": None, "envs": None, "user": "root"}
    ]


def test_commands_adapter_passes_a_zero_exit_through(monkeypatch) -> None:
    module, _ = _install_fake_e2b(monkeypatch)
    sandbox = _create(_factory())
    module.Sandbox.created[-1].commands.result = ("ok", "", 0)

    result = sandbox.commands.run("true", timeout=5)

    assert (result.stdout, result.stderr, result.exit_code) == ("ok", "", 0)


def test_commands_adapter_reraises_a_transport_failure(monkeypatch) -> None:
    module, _ = _install_fake_e2b(monkeypatch)
    sandbox = _create(_factory())
    module.Sandbox.created[-1].commands.error = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        sandbox.commands.run("ls", timeout=5)


def test_files_adapter_maps_entry_info_and_keeps_native_bytes(monkeypatch) -> None:
    module, _ = _install_fake_e2b(monkeypatch)
    sandbox = _create(_factory())
    files = module.Sandbox.created[-1].files
    files.entries = {
        "/mnt/user-data": [
            {
                "name": "workspace",
                "type": "dir",
                "size": 4096,
                "path": "/mnt/user-data/workspace",
            },
            {
                "name": "notes.md",
                "type": "file",
                "size": 12,
                "path": "/mnt/user-data/notes.md",
            },
        ]
    }

    assert sandbox.files.read("/mnt/user-data/notes.md", format="bytes") == b"file-bytes"
    assert sandbox.files.read("/mnt/user-data/notes.md") == "file-text"
    assert sandbox.files.list("/mnt/user-data") == [
        {
            "name": "workspace",
            "type": "FILE_TYPE_DIRECTORY",
            "size": "4096",
            "path": "/mnt/user-data/workspace",
        },
        {
            "name": "notes.md",
            "type": "FILE_TYPE_FILE",
            "size": "12",
            "path": "/mnt/user-data/notes.md",
        },
    ]
    assert sandbox.files.stat("/mnt/user-data/notes.md") == {
        "name": "notes.md",
        "type": "FILE_TYPE_FILE",
        "size": "12",
        "path": "/mnt/user-data/notes.md",
    }


# -- error classification --------------------------------------------------


class _SandboxException(Exception):
    """Stand-in for e2b.SandboxException: a status code, when there was one."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SandboxNotFoundException(_SandboxException):
    pass


class SandboxNotRunningException(_SandboxException):
    pass


class SandboxUnreachableException(_SandboxException):
    pass


class FileNotFoundException(_SandboxException):
    pass


def test_error_status_reads_status_code() -> None:
    assert error_status(_SandboxException("boom", 404)) == 404


def test_error_status_without_status_code_is_none() -> None:
    assert error_status(RuntimeError("boom")) is None


def test_e2b_sandbox_exceptions_are_gone() -> None:
    assert is_gone_error(SandboxNotFoundException("gone")) is True
    assert is_gone_error(SandboxNotRunningException("gone")) is True


def test_a_missing_file_is_not_a_gone_sandbox() -> None:
    missing = FileNotFoundException("no such file")

    assert is_gone_error(missing) is False
    assert is_missing_path_error(missing) is True


def test_a_gone_sandbox_is_never_a_missing_path() -> None:
    assert is_missing_path_error(SandboxNotRunningException("gone")) is False


def test_status_codes_still_classify_plain_errors() -> None:
    assert is_gone_error(_SandboxException("gone", 410)) is True
    assert is_gone_error(_SandboxException("server error", 500)) is False
    # A bare 404 answers both questions. A command reads it as "gone" (drop the
    # handle); a path request reads it as "no such file".
    assert is_gone_error(_SandboxException("missing", 404)) is True
    assert is_missing_path_error(_SandboxException("missing", 404)) is True


def test_unreachable_is_reported_separately() -> None:
    assert is_unreachable_error(SandboxUnreachableException("down")) is True
    assert is_unreachable_error(SandboxNotFoundException("gone")) is False


def test_gone_error_is_an_oserror_for_the_file_contract() -> None:
    assert issubclass(SandboxGoneError, OSError)


# -- fake control plane ----------------------------------------------------


class _StateValue:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"SandboxState.{self.value.upper()}"


def _install_fake_e2b(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[list[dict]] | None = None,
) -> tuple[types.ModuleType, dict]:
    """Install a minimal `e2b` module and record every call it receives."""

    module = types.ModuleType("e2b")
    calls: dict = {}
    remaining = [list(page) for page in (pages if pages is not None else [[]])]

    class FakeEntry:
        def __init__(self, payload: dict) -> None:
            self.name = payload["name"]
            self.type = payload["type"]
            self.size = payload["size"]
            self.path = payload["path"]

    class FakeInfo:
        def __init__(self, payload: dict) -> None:
            self.sandbox_id = payload["sandbox_id"]
            self.metadata = dict(payload.get("metadata") or {})
            self.state = payload.get("state")
            self.started_at = payload.get("started_at")

    class FakePaginator:
        @property
        def has_next(self) -> bool:
            return bool(remaining)

        def next_items(self) -> list:
            calls["list_calls"] = calls.get("list_calls", 0) + 1
            page = remaining.pop(0) if remaining else []
            return [FakeInfo(item) for item in page]

    class FakeFiles:
        def __init__(self) -> None:
            self.entries: dict[str, list[dict]] = {}
            self.requests: list[dict] = []

        def read(self, path, format="text", user=None, **kwargs):
            self.requests.append({"op": "read", "path": path, "format": format, "user": user})
            if format == "bytes":
                return b"file-bytes"
            return "file-text"

        def write(self, path, data, user=None, **kwargs):
            self.requests.append({"op": "write", "path": path, "user": user})

        def list(self, path, depth=1, user=None, **kwargs):
            self.requests.append(
                {"op": "list", "path": path, "depth": depth, "user": user}
            )
            return [FakeEntry(item) for item in self.entries.get(path, [])]

        def get_info(self, path, user=None, **kwargs):
            self.requests.append({"op": "get_info", "path": path, "user": user})
            for entries in self.entries.values():
                for item in entries:
                    if item["path"] == path:
                        return FakeEntry(item)
            raise AssertionError(f"get_info called for an unknown path: {path}")

    class FakeCommands:
        def __init__(self) -> None:
            self.result: tuple[str, str, int] | None = None
            self.error: BaseException | None = None
            self.calls: list[dict] = []

        def run(self, cmd, **kwargs):
            self.calls.append({"cmd": cmd, **kwargs})
            if self.error is not None:
                raise self.error
            stdout, stderr, exit_code = self.result or ("ok", "", 0)
            if exit_code != 0:
                # E2B raises instead of returning a non-zero result.
                raise module.CommandExitException(stderr, stdout, exit_code, None)
            return types.SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)

    class FakeSandbox:
        created: list = []

        def __init__(self, sandbox_id: str = "created-1") -> None:
            self.sandbox_id = sandbox_id
            self.commands = FakeCommands()
            self.files = FakeFiles()
            self.paused = False
            self.killed = False

        @classmethod
        def create(cls, **kwargs):
            calls["create"] = kwargs
            sandbox = cls()
            cls.created.append(sandbox)
            return sandbox

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            calls["connect"] = {"sandbox_id": sandbox_id, **kwargs}
            sandbox = cls(sandbox_id)
            cls.created.append(sandbox)
            return sandbox

        @classmethod
        def list(cls, query=None, limit=None, **kwargs):
            calls["query"] = (
                None
                if query is None
                else {
                    "metadata": dict(query.metadata or {}),
                    "state": [state.value for state in query.state or []],
                }
            )
            calls["list_limit"] = limit
            calls["list_opts"] = kwargs
            return FakePaginator()

        def pause(self) -> None:
            self.paused = True

        def kill(self) -> None:
            self.killed = True

    class FakeQuery:
        def __init__(self, metadata=None, state=None, **kwargs):
            del kwargs
            self.metadata = metadata
            self.state = state

    class FakeState:
        RUNNING = _StateValue("running")
        PAUSED = _StateValue("paused")

    class CommandExitException(Exception):
        """E2B's CommandExitException: a result that is also an exception."""

        def __init__(self, stderr, stdout, exit_code, error):
            super().__init__(stderr)
            self.stderr = stderr
            self.stdout = stdout
            self.exit_code = exit_code
            self.error = error

    module.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    module.SandboxQuery = FakeQuery  # type: ignore[attr-defined]
    module.SandboxState = FakeState  # type: ignore[attr-defined]
    module.CommandExitException = CommandExitException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "e2b", module)
    return module, calls
