"""CubeSandbox commands and file I/O contract tests.

The real ``cubesandbox`` 0.7.3 ``Filesystem.read(path, *, user=None) -> str``
has no ``format`` argument, so binary downloads go through a bounded
``dd`` + ``base64 -w0`` transfer over ``commands.run``. The fake SDK is more
permissive than the real one; these tests drive commands through the fake
command handler and never rely on ``files.read(format="bytes")``.
"""

from __future__ import annotations

import base64
import re

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


def _sandbox(tmp_path) -> CubeSandbox:
    sdk = FakeSdkSandbox(
        files=FakeFiles({_USER_DATA: tmp_path / "user-data"}),
        commands=FakeCommands(),
    )
    mounts = [MountEntry(_USER_DATA, str(tmp_path / "user-data"), False)]
    return CubeSandbox(sdk, sandbox_id="cube:s1", mounts=mounts, command_timeout=600)


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
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("AAE=")
    assert sandbox.download_file("/mnt/user-data/workspace/a.bin") == b"\x00\x01"


def test_download_file_reads_bounded_base64_chunks(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    data = bytes(range(256)) * 1200  # 307_200 bytes: two 256 KiB slices
    sandbox.update_file("/mnt/user-data/workspace/big.bin", data)

    commands: list[str] = []
    reads: list[str] = []

    def handler(cmd, **kw):
        match = re.search(r"skip=(\d+)", cmd)
        assert match is not None, cmd
        offset = int(match.group(1)) * 262_144
        commands.append(cmd)
        return FakeCommandResult(base64.b64encode(data[offset : offset + 262_144]).decode())

    sandbox._sdk.commands.handler = handler
    original_read = sandbox._sdk.files.read

    def spy(path, **kwargs):
        reads.append(path)
        return original_read(path, **kwargs)

    sandbox._sdk.files.read = spy
    assert sandbox.download_file("/mnt/user-data/workspace/big.bin") == data
    assert len(commands) == 2
    assert all("dd if=" in cmd and "base64 -w0" in cmd for cmd in commands)
    assert reads == []


def test_download_file_missing_is_oserror(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(OSError):
        sandbox.download_file("/mnt/user-data/workspace/missing.bin")


def test_download_file_rejects_a_directory(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    (tmp_path / "user-data" / "workspace").mkdir(parents=True, exist_ok=True)
    with pytest.raises(OSError):
        sandbox.download_file("/mnt/user-data/workspace")


def test_download_file_without_size_reads_until_empty(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.files.stat = lambda path, **kwargs: {"type": "FILE_TYPE_FILE"}
    chunks = iter(["AAE=", ""])
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(next(chunks))
    assert sandbox.download_file("/mnt/user-data/workspace/a.bin") == b"\x00\x01"


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
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("c2hhcmVk")
    assert sandbox.download_file(f"{_PRIVATE_SKILLS}/a.txt") == b"shared"
