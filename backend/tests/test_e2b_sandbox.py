"""E2BSandbox commands, file I/O, and lazy skill upload contract tests.

The E2B file API exposes native text and bytes modes.  There is no host mount:
the fake filesystem is the sandbox filesystem, and skill sources are uploaded on
first read.
"""

from __future__ import annotations

import json

import pytest

from agentkit.sandbox.e2b.sandbox import E2BSandbox
from agentkit.sandbox.e2b.sync import SkillSource, SyncLimits
from agentkit.sandbox.exceptions import (
    SandboxNotFoundError,
    SandboxRuntimeError,
    SandboxTimeoutError,
)
from agentkit.sandbox.security import PathMapping
from fakes.e2b_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox

_USER_DATA = "/mnt/user-data"
_SKILLS = "/mnt/skills"
_PRIVATE_SKILLS = "/mnt/skills/private"


def _sandbox(
    tmp_path,
    *,
    on_dead=None,
    skill_sources=(),
    sync_limits: SyncLimits | None = None,
) -> E2BSandbox:
    sdk = FakeSdkSandbox(files=FakeFiles(), commands=FakeCommands())
    mappings = [PathMapping(_USER_DATA, str(tmp_path / "user-data"), False)]
    if skill_sources:
        mappings.append(PathMapping(_SKILLS, str(tmp_path / "skills"), True))
    return E2BSandbox(
        sdk,
        sandbox_id="e2b:s1",
        path_mappings=mappings,
        skill_sources=skill_sources,
        sync_limits=sync_limits,
        command_timeout=600,
        on_dead=on_dead,
    )


class _ApiError(Exception):
    """Stand-in for an E2B API error carrying an HTTP status."""

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


def _namespaced_sandbox(tmp_path) -> E2BSandbox:
    sdk = FakeSdkSandbox(files=FakeFiles(), commands=FakeCommands())
    mappings = [
        PathMapping(_USER_DATA, str(tmp_path / "user-data"), False),
        PathMapping(_PRIVATE_SKILLS, str(tmp_path / "private"), True),
    ]
    return E2BSandbox(sdk, sandbox_id="e2b:s1", path_mappings=mappings)


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

    def raise_404(cmd, **kw):
        raise _ApiError(404)

    sandbox._sdk.commands.handler = raise_404
    with pytest.raises(SandboxNotFoundError):
        sandbox.execute_command("true")

    def raise_auth(cmd, **kw):
        raise _ApiError(403)

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

    assert dead == ["e2b:s1"]


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
    assert sandbox.id == "e2b:s1"
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
    assert sandbox.read_file("/mnt/user-data/workspace/sub/a.txt") == "x"
    assert any("mkdir -p" in call["cmd"] for call in sandbox._sdk.commands.calls)


def test_write_file_append_creates_a_missing_file(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file("/mnt/user-data/workspace/new.txt", "first\n", append=True)
    assert sandbox.read_file("/mnt/user-data/workspace/new.txt") == "first\n"


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
    assert sandbox.read_file("/mnt/user-data/workspace/a.txt") == "one\ntwo\n"


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
    from agentkit.sandbox.e2b.files import read_bytes

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
    sandbox._sdk.files.make_dir("/mnt/user-data/workspace")
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


# -- lazy skill upload ----------------------------------------------------


def _skill_tree(tmp_path):
    root = tmp_path / "skills"
    (root / "private" / "demo").mkdir(parents=True)
    (root / "private" / "demo" / "SKILL.md").write_text("skill body\n", encoding="utf-8")
    (root / "private" / "demo" / "notes.md").write_text("notes\n", encoding="utf-8")
    return root


def test_read_file_uploads_one_skill_file_on_demand(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
    )

    assert sandbox.read_file("/mnt/skills/private/demo/SKILL.md").strip() == "skill body"
    assert "/mnt/skills/private/demo/SKILL.md" in sandbox._sdk.files.writes
    # The neighbouring file stays out of the sandbox.
    assert "/mnt/skills/private/demo/notes.md" not in sandbox._sdk.files.writes


def test_a_second_read_uses_the_uploaded_copy(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
    )

    sandbox.read_file("/mnt/skills/private/demo/SKILL.md")
    sandbox.read_file("/mnt/skills/private/demo/SKILL.md")

    assert sandbox._sdk.files.writes.count("/mnt/skills/private/demo/SKILL.md") == 1


def test_list_dir_uploads_the_queried_subtree_once(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
    )

    lines = sandbox.list_dir("/mnt/skills/private/demo")
    assert lines == ["├── notes.md", "└── SKILL.md"]
    assert "/mnt/skills/private/demo/notes.md" in sandbox._sdk.files.writes
    assert "/mnt/skills/private/demo/SKILL.md" in sandbox._sdk.files.writes

    writes = list(sandbox._sdk.files.writes)
    sandbox.list_dir("/mnt/skills/private/demo")
    assert sandbox._sdk.files.writes == writes


def test_glob_uploads_the_subtree_before_running_the_helper(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
    )
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(
        json.dumps({"matches": ["/mnt/skills/private/demo/SKILL.md"], "truncated": False})
    )

    matches, truncated = sandbox.glob("/mnt/skills/private/demo", "**/*.md")
    assert matches == ["/mnt/skills/private/demo/SKILL.md"]
    assert truncated is False
    assert "/mnt/skills/private/demo/SKILL.md" in sandbox._sdk.files.writes
    assert "/mnt/skills/private/demo/notes.md" in sandbox._sdk.files.writes


def test_grep_uploads_the_subtree_before_running_the_helper(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
    )
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(
        json.dumps(
            {
                "matches": [
                    {
                        "path": "/mnt/skills/private/demo/SKILL.md",
                        "line_number": 1,
                        "line": "skill body",
                    }
                ],
                "truncated": False,
            }
        )
    )

    matches, _ = sandbox.grep("/mnt/skills/private/demo", "skill", literal=True)
    assert matches[0].line == "skill body"
    assert "/mnt/skills/private/demo/SKILL.md" in sandbox._sdk.files.writes


def test_oversized_skill_tree_is_an_explicit_error(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
        sync_limits=SyncLimits(max_tree_bytes=4, max_file_bytes=1024),
    )

    with pytest.raises(OSError, match="cannot upload skills on demand"):
        sandbox.list_dir("/mnt/skills/private/demo")


def test_oversized_skill_file_is_an_explicit_error(tmp_path) -> None:
    root = _skill_tree(tmp_path)
    sandbox = _sandbox(
        tmp_path,
        skill_sources=(SkillSource(root, _SKILLS),),
        sync_limits=SyncLimits(max_file_bytes=4, max_tree_bytes=1024),
    )

    with pytest.raises(OSError, match="cannot upload skills on demand"):
        sandbox.read_file("/mnt/skills/private/demo/SKILL.md")


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

    assert dead == ["e2b:s1"]


def test_a_file_read_of_a_gone_sandbox_drops_the_handle(tmp_path) -> None:
    dead: list[str] = []
    sandbox = _sandbox(tmp_path, on_dead=lambda: dead.append(sandbox.id))
    sandbox._sdk.files.read = _raise(SandboxNotRunningException("was not found"))

    with pytest.raises(OSError) as exc_info:
        sandbox.read_file("/mnt/user-data/workspace/a.txt")

    # The Sandbox file contract stays OSError-shaped for the tool layer.
    assert not isinstance(exc_info.value, FileNotFoundError)
    assert dead == ["e2b:s1"]


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
