"""OpenSandboxSandbox: the Sandbox contract over a fake handle.

Same contract as local/cube: exit codes are tool output, a missing path stays a
FileNotFoundError, path escapes are rejected, and a transport failure is reported
to the owner exactly once.
"""

from __future__ import annotations

import pytest

from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.host_mounts import MountEntry
from agentkit.sandbox.opensandbox.client import (
    CommandOutput,
    Entry,
    OsbTransportError,
)
from agentkit.sandbox.opensandbox.sandbox import OpenSandboxSandbox
from fakes.osb_client import FakeOsbHandle

_USER_DATA = "/mnt/user-data"
_PRIVATE_SKILLS = "/mnt/skills/private"


def _sandbox(tmp_path, handle=None, **kwargs) -> OpenSandboxSandbox:
    active = handle if handle is not None else FakeOsbHandle()
    mounts = [MountEntry(_USER_DATA, str(tmp_path / "user-data"), False)]
    return OpenSandboxSandbox(active, sandbox_id="osb:u1:t1", mounts=mounts, **kwargs)


def _namespaced(tmp_path, handle=None, **kwargs) -> OpenSandboxSandbox:
    active = handle if handle is not None else FakeOsbHandle()
    (tmp_path / "private").mkdir(parents=True, exist_ok=True)
    mounts = [
        MountEntry(_USER_DATA, str(tmp_path / "user-data"), False),
        MountEntry(_PRIVATE_SKILLS, str(tmp_path / "private"), True),
    ]
    return OpenSandboxSandbox(active, sandbox_id="osb:u1:t1", mounts=mounts, **kwargs)


# -- commands ---------------------------------------------------------------


def test_command_returns_text_and_appends_the_exit_code(tmp_path) -> None:
    handle = FakeOsbHandle(command_output=CommandOutput("hi", 2))
    sandbox = _sandbox(tmp_path, handle)

    assert sandbox.execute_command("false") == "hi\nexit code 2"
    assert handle.command_calls[0]["timeout"] == 600
    assert handle.command_calls[0]["max_output_bytes"] == 1024 * 1024


def test_command_with_no_output_still_reports_the_exit_code(tmp_path) -> None:
    sandbox = _sandbox(tmp_path, FakeOsbHandle(command_output=CommandOutput("", 3)))

    assert sandbox.execute_command("false") == "exit code 3"


def test_command_passes_env_and_timeout(tmp_path) -> None:
    handle = FakeOsbHandle()
    sandbox = _sandbox(tmp_path, handle)

    sandbox.execute_command("true", env={"MYFLOW_TOKEN": "t"}, timeout=42)

    assert handle.command_calls[0]["timeout"] == 42
    assert handle.command_calls[0]["env"] == {"MYFLOW_TOKEN": "t"}


def test_command_rejects_an_illegal_env_key(tmp_path) -> None:
    with pytest.raises(ValueError):
        _sandbox(tmp_path).execute_command("true", env={"1bad": "x"})


def test_command_masks_host_paths(tmp_path) -> None:
    host_file = str(tmp_path / "user-data" / "workspace" / "a.txt")
    sandbox = _sandbox(
        tmp_path, FakeOsbHandle(command_output=CommandOutput("wrote " + host_file, 0))
    )

    masked = sandbox.execute_command("true")

    assert masked.startswith("wrote /mnt/user-data")
    assert host_file not in masked


def test_declares_no_persistent_shell(tmp_path) -> None:
    assert _sandbox(tmp_path).persistent_shell_sessions is False


def test_scope_calls_share_the_fresh_process_model(tmp_path) -> None:
    handle = FakeOsbHandle(command_output=CommandOutput("ok", 0))
    sandbox = _sandbox(tmp_path, handle)

    assert sandbox.execute_command_in_scope("true", scope="subagent:1") == "ok"
    assert len(handle.command_calls) == 1


# -- files ------------------------------------------------------------------


def test_read_file_truncates_long_text(tmp_path) -> None:
    sandbox = _sandbox(tmp_path, FakeOsbHandle(text="x" * 150_000))

    text = sandbox.read_file(f"{_USER_DATA}/workspace/a.txt")

    assert len(text) < 150_000
    assert "truncated" in text


def test_read_file_slices_lines_locally(tmp_path) -> None:
    sandbox = _sandbox(tmp_path, FakeOsbHandle(text="one\ntwo\nthree\n"))

    assert sandbox.read_file(f"{_USER_DATA}/a.txt", 2, 2) == "two"
    assert sandbox.read_file(f"{_USER_DATA}/a.txt", start_line=2) == "two\nthree"


def test_read_file_missing_is_oserror(tmp_path) -> None:
    sandbox = _sandbox(
        tmp_path, FakeOsbHandle(text_error=FileNotFoundError("/mnt/user-data/x"))
    )

    with pytest.raises(OSError):
        sandbox.read_file(f"{_USER_DATA}/workspace/missing.txt")


def test_write_file_creates_the_parent_directory_first(tmp_path) -> None:
    handle = FakeOsbHandle()
    sandbox = _sandbox(tmp_path, handle)

    sandbox.write_file(f"{_USER_DATA}/workspace/sub/a.txt", "x")

    assert handle.dirs_created == [f"{_USER_DATA}/workspace/sub"]
    assert handle.writes == [{"path": f"{_USER_DATA}/workspace/sub/a.txt", "content": "x"}]


def test_write_file_skips_mkdir_for_a_mount_root(tmp_path) -> None:
    handle = FakeOsbHandle()
    sandbox = _sandbox(tmp_path, handle)

    sandbox.write_file(f"{_USER_DATA}/a.txt", "x")

    assert handle.dirs_created == []
    assert handle.writes[0]["path"] == f"{_USER_DATA}/a.txt"


def test_write_file_append_merges_existing_content(tmp_path) -> None:
    handle = FakeOsbHandle(root_map={_USER_DATA: str(tmp_path / "user-data")})
    (tmp_path / "user-data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user-data" / "a.txt").write_text("one", encoding="utf-8")
    sandbox = _sandbox(tmp_path, handle)

    sandbox.write_file(f"{_USER_DATA}/a.txt", "two", append=True)

    assert (tmp_path / "user-data" / "a.txt").read_text(encoding="utf-8") == "onetwo"


def test_bytes_round_trip(tmp_path) -> None:
    handle = FakeOsbHandle(byte_content=b"\x00\x01")
    sandbox = _sandbox(tmp_path, handle)

    sandbox.update_file(f"{_USER_DATA}/workspace/a.bin", b"\x00\x01")
    assert sandbox.download_file(f"{_USER_DATA}/workspace/a.bin") == b"\x00\x01"
    assert handle.byte_writes[0]["data"] == b"\x00\x01"


def test_download_failure_is_oserror(tmp_path) -> None:
    sandbox = _sandbox(
        tmp_path, FakeOsbHandle(bytes_error=FileNotFoundError("/mnt/user-data/x"))
    )

    with pytest.raises(OSError):
        sandbox.download_file(f"{_USER_DATA}/workspace/missing.bin")


# -- listing and search ------------------------------------------------------


def test_list_dir_renders_the_same_tree_as_the_other_providers(tmp_path) -> None:
    handle = FakeOsbHandle(
        entries=[
            Entry(name="a.txt", path=f"{_USER_DATA}/workspace/a.txt", is_dir=False),
            Entry(name="sub", path=f"{_USER_DATA}/workspace/sub", is_dir=True),
            Entry(name="b.txt", path=f"{_USER_DATA}/workspace/sub/b.txt", is_dir=False),
        ]
    )
    sandbox = _sandbox(tmp_path, handle)

    assert sandbox.list_dir(f"{_USER_DATA}/workspace") == [
        "├── sub/",
        "│   └── b.txt",
        "└── a.txt",
    ]


def test_list_dir_stops_at_max_depth(tmp_path) -> None:
    handle = FakeOsbHandle(
        entries=[
            Entry(name="sub", path=f"{_USER_DATA}/sub", is_dir=True),
            Entry(name="deep.txt", path=f"{_USER_DATA}/sub/deep.txt", is_dir=False),
        ]
    )
    sandbox = _sandbox(tmp_path, handle)

    assert sandbox.list_dir(f"{_USER_DATA}", max_depth=1) == ["└── sub/"]


def test_list_dir_missing_is_file_not_found(tmp_path) -> None:
    sandbox = _sandbox(
        tmp_path, FakeOsbHandle(list_error=FileNotFoundError("/mnt/user-data/missing"))
    )

    with pytest.raises(FileNotFoundError):
        sandbox.list_dir(f"{_USER_DATA}/missing")


def test_list_dir_of_a_file_is_not_a_directory(tmp_path) -> None:
    sandbox = _sandbox(
        tmp_path, FakeOsbHandle(list_error=NotADirectoryError("/mnt/user-data/a.txt"))
    )

    with pytest.raises(NotADirectoryError):
        sandbox.list_dir(f"{_USER_DATA}/a.txt")


def test_empty_directory_returns_no_lines(tmp_path) -> None:
    assert _sandbox(tmp_path, FakeOsbHandle(entries=[])).list_dir(_USER_DATA) == []


def test_glob_filters_directories_and_reports_truncation(tmp_path) -> None:
    handle = FakeOsbHandle(
        search_result=(
            [
                Entry(name="a.py", path=f"{_USER_DATA}/a.py", is_dir=False),
                Entry(name="pkg", path=f"{_USER_DATA}/pkg", is_dir=True),
            ],
            True,
        )
    )
    sandbox = _sandbox(tmp_path, handle)

    assert sandbox.glob(_USER_DATA, "**/*.py", max_results=5) == ([f"{_USER_DATA}/a.py"], True)
    assert sandbox.glob(_USER_DATA, "**", include_dirs=True)[0] == [
        f"{_USER_DATA}/a.py",
        f"{_USER_DATA}/pkg",
    ]


def test_grep_builds_a_bounded_grep_and_parses_matches(tmp_path) -> None:
    handle = FakeOsbHandle(
        command_output=CommandOutput(f"{_USER_DATA}/a.py:3:needle\n", 0)
    )
    sandbox = _sandbox(tmp_path, handle)

    matches, truncated = sandbox.grep(
        _USER_DATA, "needle", glob="*.py", literal=True, case_sensitive=False, max_results=7
    )

    command = handle.command_calls[0]["command"]
    assert command.startswith("grep -r -n -I")
    assert " -F" in command
    assert " -i" in command
    assert "--include" in command
    assert "| head -n 8" in command
    assert [(m.path, m.line_number, m.line) for m in matches] == [
        (f"{_USER_DATA}/a.py", 3, "needle")
    ]
    assert truncated is False


def test_grep_marks_truncation_at_the_limit(tmp_path) -> None:
    lines = "".join(f"{_USER_DATA}/a.py:{index}:hit\n" for index in range(1, 4))
    handle = FakeOsbHandle(command_output=CommandOutput(lines, 0))
    sandbox = _sandbox(tmp_path, handle)

    matches, truncated = sandbox.grep(_USER_DATA, "hit", max_results=2)

    assert len(matches) == 2
    assert truncated is True


def test_grep_case_sensitive_omits_the_flag(tmp_path) -> None:
    handle = FakeOsbHandle()
    sandbox = _sandbox(tmp_path, handle)

    sandbox.grep(_USER_DATA, "x", case_sensitive=True)

    assert " -i" not in handle.command_calls[0]["command"]


# -- path guard --------------------------------------------------------------


def test_paths_outside_the_mounts_are_rejected(tmp_path) -> None:
    sandbox = _namespaced(tmp_path)

    with pytest.raises(PermissionError):
        sandbox.read_file(f"{_USER_DATA}/../skills/x")
    with pytest.raises(PermissionError):
        sandbox.read_file("/etc/passwd")
    with pytest.raises(PermissionError):
        sandbox.write_file(f"{_PRIVATE_SKILLS}/a.txt", "x")
    with pytest.raises(PermissionError):
        sandbox.update_file(f"{_PRIVATE_SKILLS}/a.bin", b"x")


def test_read_only_mounts_stay_readable(tmp_path) -> None:
    sandbox = _namespaced(tmp_path, FakeOsbHandle(text="shared"))

    assert sandbox.read_file(f"{_PRIVATE_SKILLS}/a.txt") == "shared"


# -- transport failure -------------------------------------------------------


def test_a_transport_failure_notifies_the_owner_once(tmp_path) -> None:
    calls: list[str] = []
    handle = FakeOsbHandle(command_error=OsbTransportError("server unreachable"))
    sandbox = _sandbox(tmp_path, handle, on_transport_failure=lambda: calls.append("gone"))

    for _ in range(3):
        with pytest.raises(SandboxError):
            sandbox.execute_command("true")

    assert calls == ["gone"]


def test_a_failing_callback_does_not_hide_the_error(tmp_path) -> None:
    def explode() -> None:
        raise RuntimeError("callback exploded")

    handle = FakeOsbHandle(command_error=OsbTransportError("server unreachable"))
    sandbox = _sandbox(tmp_path, handle, on_transport_failure=explode)

    with pytest.raises(SandboxError):
        sandbox.execute_command("true")


def test_transport_failures_stay_oserror_for_files(tmp_path) -> None:
    handle = FakeOsbHandle(text_error=OsbTransportError("server unreachable"))
    sandbox = _sandbox(tmp_path, handle)

    with pytest.raises(OSError):
        sandbox.read_file(f"{_USER_DATA}/workspace/a.txt")
