"""CubeSandbox commands and file I/O contract tests.

The E2B file API exposes native text and bytes modes. Binary downloads use
``files.read(format="bytes")``; the fake SDK implements the same mode so these
tests exercise the production path.
"""

from __future__ import annotations

import pytest

from agentkit.sandbox.cube.mounts import MountEntry
from agentkit.sandbox.cube.sandbox import CubeSandbox
from agentkit.sandbox.exceptions import (
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxTimeoutError,
)
from fakes.cube_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox

_USER_DATA = "/mnt/user-data"
_PRIVATE_SKILLS = "/mnt/skills/private"


def _sandbox(tmp_path, *, on_dead=None) -> CubeSandbox:
    sdk = FakeSdkSandbox(
        files=FakeFiles({_USER_DATA: tmp_path / "user-data"}),
        commands=FakeCommands(),
    )
    mounts = [MountEntry(_USER_DATA, str(tmp_path / "user-data"), False)]
    return CubeSandbox(
        sdk,
        sandbox_id="cube:s1",
        mounts=mounts,
        command_timeout=600,
        on_dead=on_dead,
    )


class _ApiError(Exception):
    """Stand-in for cubesandbox.ApiError carrying an HTTP status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _raise_status(status_code: int):
    def handler(cmd, **kwargs):
        del cmd, kwargs
        raise _ApiError(status_code)

    return handler


def _boom() -> None:
    raise RuntimeError("the on_dead callback exploded")


def _namespaced_sandbox(tmp_path) -> CubeSandbox:
    (tmp_path / "private").mkdir(parents=True, exist_ok=True)
    sdk = FakeSdkSandbox(
        files=FakeFiles(
            {
                _USER_DATA: tmp_path / "user-data",
                _PRIVATE_SKILLS: tmp_path / "private",
            }
        ),
        commands=FakeCommands(),
    )
    mounts = [
        MountEntry(_USER_DATA, str(tmp_path / "user-data"), False),
        MountEntry(_PRIVATE_SKILLS, str(tmp_path / "private"), True),
    ]
    return CubeSandbox(sdk, sandbox_id="cube:s1", mounts=mounts)


def test_execute_command_returns_stdout_and_exit_code(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("hi\n", "", 2)
    assert sandbox.execute_command("echo hi") == "hi\nexit code 2"


def test_command_wraps_output_and_uses_600s_timeout(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    seen = {}
    sandbox._sdk.commands.handler = lambda cmd, **kw: (
        seen.update(cmd=cmd, timeout=kw["timeout"]) or FakeCommandResult()
    )
    sandbox.execute_command("echo hi")
    assert "head -c 1048576" in seen["cmd"]
    assert "PIPESTATUS" in seen["cmd"]
    assert seen["timeout"] == 600


def test_command_passes_env_user_and_timeout_override(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("ok")
    sandbox.execute_command("true", env={"MYFLOW_TOKEN": "t"}, timeout=42)
    call = sandbox._sdk.commands.calls[-1]
    assert call["envs"] == {"MYFLOW_TOKEN": "t"}
    assert call["user"] == "root"
    assert call["timeout"] == 42


def test_command_rejects_illegal_env_key(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(ValueError):
        sandbox.execute_command("true", env={"1bad": "x"})


def test_command_maps_sigpipe_exit_to_success(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("part", "", 141)
    assert sandbox.execute_command("yes") == "part"


def test_command_maps_timeout_and_gone_and_auth(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)

    class TimeoutException(Exception):
        pass

    def raise_timeout(cmd, **kw):
        raise TimeoutException("read timed out")

    sandbox._sdk.commands.handler = raise_timeout
    with pytest.raises(SandboxTimeoutError):
        sandbox.execute_command("sleep 5")

    class ApiError(Exception):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    def raise_404(cmd, **kw):
        raise ApiError(404)

    sandbox._sdk.commands.handler = raise_404
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    def raise_auth(cmd, **kw):
        raise ApiError(403)

    sandbox._sdk.commands.handler = raise_auth
    with pytest.raises(SandboxRuntimeError):
        sandbox.execute_command("true")


@pytest.mark.parametrize("status", [404, 410])
def test_command_gone_marks_the_handle_dead_once(tmp_path, status) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))
    sandbox._sdk.commands.handler = _raise_status(status)

    for _ in range(3):
        with pytest.raises(SandboxNotFoundError):
            sandbox.execute_command("true")

    assert dead == ["cube:s1"]


def test_command_failures_that_are_not_gone_leave_the_handle_alive(tmp_path) -> None:
    dead: list[object] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(None))

    sandbox._sdk.commands.handler = _raise_status(500)
    with pytest.raises(SandboxRuntimeError):
        sandbox.execute_command("true")

    sandbox._sdk.commands.handler = _raise_status(401)
    with pytest.raises(SandboxRuntimeError):
        sandbox.execute_command("true")

    class TimeoutException(Exception):
        pass

    def raise_timeout(cmd, **kwargs):
        del cmd, kwargs
        raise TimeoutException("read timed out")

    sandbox._sdk.commands.handler = raise_timeout
    with pytest.raises(SandboxTimeoutError):
        sandbox.execute_command("true")

    assert dead == []


def test_failing_on_dead_callback_does_not_hide_not_found(tmp_path) -> None:
    sandbox = _sandbox(tmp_path, on_dead=_boom)
    sandbox._sdk.commands.handler = _raise_status(404)
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")


def test_command_masks_host_paths(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    host_file = str(tmp_path / "user-data" / "workspace" / "a.txt")
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(f"wrote {host_file}")
    masked = sandbox.execute_command("true")
    assert masked.startswith("wrote /mnt/user-data")
    assert host_file not in masked


def test_path_mappings_and_persistent_shell(tmp_path) -> None:
    sandbox = _namespaced_sandbox(tmp_path)
    assert sandbox.persistent_shell_sessions is False
    assert sandbox.id == "cube:s1"
    mappings = [
        (mapping.container_path, mapping.local_path, mapping.read_only)
        for mapping in sandbox.path_mappings
    ]
    assert mappings == [
        (_USER_DATA, str(tmp_path / "user-data"), False),
        (_PRIVATE_SKILLS, str(tmp_path / "private"), True),
    ]


def test_read_file_line_range(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "one\ntwo\nthree\n")
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt", 2, 2) == "two"
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt") == "one\ntwo\nthree\n"
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt", start_line=3) == "three"


def test_read_file_uses_text_api_without_format(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "hello")
    seen: dict[str, object] = {}
    original = sandbox._sdk.files.read

    def spy(path, **kwargs):
        seen.update(path=path, kwargs=kwargs)
        return original(path, **kwargs)

    sandbox._sdk.files.read = spy
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt") == "hello"
    assert seen["path"] == "/mnt/user-data/workspace/a.txt"
    assert "format" not in seen["kwargs"]
    assert seen["kwargs"]["user"] == "root"


def test_read_file_missing_is_oserror(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(OSError):
        sandbox.read_file("/mnt/user-data/workspace/missing.txt")


def test_read_file_large_range_uses_sed(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/big.txt", "line\n" * 400_000)
    seen: dict[str, str] = {}

    def handler(cmd, **kw):
        seen["cmd"] = cmd
        return FakeCommandResult("line\n")

    sandbox._sdk.commands.handler = handler
    assert sandbox.read_file("/mnt/user-data/workspace/big.txt", 10, 10) == "line"
    assert "sed -n" in seen["cmd"]
    assert "head -c" in seen["cmd"]


def test_write_file_creates_parent(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/sub/a.txt", "x")
    assert (tmp_path / "user-data" / "workspace" / "sub" / "a.txt").read_text() == "x"
    assert any("mkdir -p" in call["cmd"] for call in sandbox._sdk.commands.calls)


def test_write_file_append_creates_a_missing_file(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/new.txt", "first\n", append=True)
    assert (tmp_path / "user-data" / "workspace" / "new.txt").read_text() == "first\n"


def test_read_file_truncates_a_huge_file(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/huge.txt", "x" * 150_000)
    text = sandbox.read_file("/mnt/user-data/workspace/huge.txt")
    assert len(text) < 150_000
    assert "truncated" in text


def test_write_file_append_reads_then_writes(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "one\n")
    sandbox.write_file("/mnt/user-data/workspace/a.txt", "two\n", append=True)
    assert (tmp_path / "user-data" / "workspace" / "a.txt").read_text() == "one\ntwo\n"


def test_download_and_update_bytes(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.update_file("/mnt/user-data/workspace/a.bin", b"\x00\x01")
    assert sandbox.download_file("/mnt/user-data/workspace/a.bin") == b"\x00\x01"


def test_download_file_uses_native_e2b_bytes_mode(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    data = bytes(range(256)) * 1200
    sandbox.update_file("/mnt/user-data/workspace/big.bin", data)
    calls: list[dict] = []
    original_read = sandbox._sdk.files.read

    def spy(path, **kwargs):
        calls.append(kwargs)
        return original_read(path, **kwargs)

    sandbox._sdk.files.read = spy

    assert sandbox.download_file("/mnt/user-data/workspace/big.bin") == data
    assert calls[-1]["format"] == "bytes"


def test_read_bytes_refuses_an_oversized_artifact_before_transfer(tmp_path) -> None:
    from agentkit.sandbox.cube.files import read_bytes

    sandbox = _sandbox(tmp_path)
    sandbox.update_file("/mnt/user-data/workspace/big.bin", b"0123456789")
    calls: list[str] = []
    original_read = sandbox._sdk.files.read

    def spy(path, **kwargs):
        calls.append(path)
        return original_read(path, **kwargs)

    sandbox._sdk.files.read = spy

    with pytest.raises(OSError, match="transfer limit"):
        read_bytes(
            sandbox._sdk,
            "/mnt/user-data/workspace/big.bin",
            user="root",
            max_bytes=4,
        )

    # The size probe rejected it, so the gateway never buffered the file.
    assert calls == []


def test_download_file_missing_is_oserror(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(OSError):
        sandbox.download_file("/mnt/user-data/workspace/missing.bin")


def test_download_file_rejects_a_directory(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    (tmp_path / "user-data" / "workspace").mkdir(parents=True, exist_ok=True)
    with pytest.raises(OSError):
        sandbox.download_file("/mnt/user-data/workspace")


def test_file_tools_reject_escape_and_read_only(tmp_path) -> None:
    sandbox = _namespaced_sandbox(tmp_path)
    with pytest.raises(PermissionError):
        sandbox.read_file("/mnt/user-data/../skills/x")
    with pytest.raises(PermissionError):
        sandbox.read_file("/etc/passwd")
    with pytest.raises(PermissionError):
        sandbox.write_file(f"{_PRIVATE_SKILLS}/a.txt", "x")
    with pytest.raises(PermissionError):
        sandbox.update_file(f"{_PRIVATE_SKILLS}/a.bin", b"x")

    (tmp_path / "private" / "a.txt").write_text("shared", encoding="utf-8")
    assert sandbox.read_file(f"{_PRIVATE_SKILLS}/a.txt") == "shared"
    assert sandbox.download_file(f"{_PRIVATE_SKILLS}/a.txt") == b"shared"


# -- E2B lifecycle errors --------------------------------------------------
#
# E2B reports "the sandbox is gone" in the exception type, not in an HTTP
# status, and "this path is missing" as a distinct type. A dead handle must be
# dropped; a missing path must stay an ordinary file error.


class SandboxNotRunningException(Exception):
    """Stand-in for e2b.SandboxNotRunningException."""


class FileNotFoundException(Exception):
    """Stand-in for e2b.FileNotFoundException."""


def _raise(exception: BaseException):
    def handler(*args, **kwargs):
        del args, kwargs
        raise exception

    return handler


def test_command_maps_an_e2b_gone_type_to_not_found(tmp_path) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))
    sandbox._sdk.commands.handler = _raise(
        SandboxNotRunningException("was not found: the sandbox was killed")
    )

    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    assert dead == ["cube:s1"]


def test_a_file_read_of_a_gone_sandbox_drops_the_handle(tmp_path) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))
    sandbox._sdk.files.read = _raise(SandboxNotRunningException("was not found"))

    with pytest.raises(OSError) as exc_info:
        sandbox.read_file("/mnt/user-data/workspace/a.txt")

    # The Sandbox file contract stays OSError-shaped for the tool layer.
    assert not isinstance(exc_info.value, FileNotFoundError)
    assert dead == ["cube:s1"]


def test_a_missing_file_type_keeps_the_handle_alive(tmp_path) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))
    sandbox._sdk.files.read = _raise(FileNotFoundException("no such file"))

    with pytest.raises(OSError):
        sandbox.read_file("/mnt/user-data/workspace/a.txt")

    assert dead == []


def test_listing_a_missing_path_is_not_a_dead_handle(tmp_path) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))

    with pytest.raises(FileNotFoundError):
        sandbox.list_dir("/mnt/user-data/missing")

    assert dead == []
