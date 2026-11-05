"""AioSandbox: the Sandbox contract over the fake data plane.

Same assertions as the CubeSandbox suite where the contract is shared, plus the
AIO-specific behaviours: exit-code rendering without a shell wrapper, the
server-side output cap, and the transport-failure signal.
"""

from __future__ import annotations

import pytest

from agentkit.sandbox.aio_sandbox.client import AioTransportError, CommandOutput, Entry, GrepResult
from agentkit.sandbox.aio_sandbox.sandbox import AioSandbox
from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.host_mounts import MountEntry
from fakes.aio_client import FakeAioClient

_USER_DATA = "/mnt/user-data"
_PRIVATE_SKILLS = "/mnt/skills/private"


def _sandbox(tmp_path, client=None, **kwargs) -> AioSandbox:
    active = client if client is not None else FakeAioClient()
    mounts = [MountEntry(_USER_DATA, str(tmp_path / "user-data"), False)]
    return AioSandbox(active, sandbox_id="aio:u1:t1", mounts=mounts, **kwargs)


def _namespaced(tmp_path, client=None, **kwargs) -> AioSandbox:
    active = client if client is not None else FakeAioClient()
    (tmp_path / "private").mkdir(parents=True, exist_ok=True)
    mounts = [
        MountEntry(_USER_DATA, str(tmp_path / "user-data"), False),
        MountEntry(_PRIVATE_SKILLS, str(tmp_path / "private"), True),
    ]
    return AioSandbox(active, sandbox_id="aio:u1:t1", mounts=mounts, **kwargs)


# -- commands ---------------------------------------------------------------


def test_command_returns_text_and_appends_the_exit_code(tmp_path) -> None:
    client = FakeAioClient(command_output=CommandOutput("hi", 2))
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.execute_command("false") == "hi\nexit code 2"
    assert client.commands[0]["timeout"] == 600
    assert client.commands[0]["max_output_bytes"] == 1024 * 1024


def test_command_with_no_output_still_reports_the_exit_code(tmp_path) -> None:
    client = FakeAioClient(command_output=CommandOutput("", 3))
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.execute_command("false") == "exit code 3"


def test_command_uses_the_per_call_timeout_and_env(tmp_path) -> None:
    client = FakeAioClient()
    sandbox = _sandbox(tmp_path, client)

    sandbox.execute_command("true", env={"MYFLOW_TOKEN": "t"}, timeout=42)

    assert client.commands[0]["timeout"] == 42
    assert client.commands[0]["env"] == {"MYFLOW_TOKEN": "t"}


def test_command_rejects_an_illegal_env_key(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)

    with pytest.raises(ValueError):
        sandbox.execute_command("true", env={"1bad": "x"})


def test_command_masks_host_paths(tmp_path) -> None:
    host_file = str(tmp_path / "user-data" / "workspace" / "a.txt")
    client = FakeAioClient(command_output=CommandOutput(f"wrote {host_file}", 0))
    sandbox = _sandbox(tmp_path, client)

    masked = sandbox.execute_command("true")

    assert masked.startswith("wrote /mnt/user-data")
    assert host_file not in masked


def test_declares_no_persistent_shell(tmp_path) -> None:
    assert _sandbox(tmp_path).persistent_shell_sessions is False


def test_scope_calls_share_the_fresh_session_model(tmp_path) -> None:
    client = FakeAioClient(command_output=CommandOutput("ok", 0))
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.execute_command_in_scope("true", scope="subagent:1") == "ok"
    assert len(client.commands) == 1


# -- files ------------------------------------------------------------------


def test_read_file_truncates_long_text(tmp_path) -> None:
    client = FakeAioClient(text="x" * 150_000)
    sandbox = _sandbox(tmp_path, client)

    text = sandbox.read_file(f"{_USER_DATA}/workspace/a.txt")

    assert len(text) < 150_000
    assert "truncated" in text


def test_read_file_missing_is_oserror(tmp_path) -> None:
    client = FakeAioClient(text_error=FileNotFoundError("/mnt/user-data/x"))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(OSError):
        sandbox.read_file(f"{_USER_DATA}/workspace/missing.txt")


def test_write_file_creates_the_parent_directory_first(tmp_path) -> None:
    client = FakeAioClient()
    sandbox = _sandbox(tmp_path, client)

    sandbox.write_file(f"{_USER_DATA}/workspace/sub/a.txt", "x")

    assert client.commands[0]["command"].startswith("mkdir -p -- ")
    assert client.writes == [
        {"path": f"{_USER_DATA}/workspace/sub/a.txt", "content": "x", "append": False}
    ]


def test_write_file_skips_mkdir_for_a_top_level_mount(tmp_path) -> None:
    client = FakeAioClient()
    sandbox = _sandbox(tmp_path, client)

    sandbox.write_file(f"{_USER_DATA}/a.txt", "x")

    assert client.commands == []
    assert client.writes[0]["path"] == f"{_USER_DATA}/a.txt"


def test_write_file_append_is_forwarded(tmp_path) -> None:
    client = FakeAioClient()
    sandbox = _sandbox(tmp_path, client)

    sandbox.write_file(f"{_USER_DATA}/workspace/a.txt", "more", append=True)

    assert client.writes[-1]["append"] is True


def test_mkdir_failure_is_oserror(tmp_path) -> None:
    client = FakeAioClient(command_output=CommandOutput("permission denied", 1))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(OSError, match="parent directory"):
        sandbox.write_file(f"{_USER_DATA}/workspace/sub/a.txt", "x")


def test_bytes_round_trip(tmp_path) -> None:
    client = FakeAioClient(byte_content=b"\x00\x01")
    sandbox = _sandbox(tmp_path, client)

    sandbox.update_file(f"{_USER_DATA}/workspace/a.bin", b"\x00\x01")
    assert sandbox.download_file(f"{_USER_DATA}/workspace/a.bin") == b"\x00\x01"
    assert client.byte_writes[0]["data"] == b"\x00\x01"


def test_download_failure_is_oserror(tmp_path) -> None:
    client = FakeAioClient(bytes_error=FileNotFoundError("/mnt/user-data/x"))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(OSError):
        sandbox.download_file(f"{_USER_DATA}/workspace/missing.bin")


# -- listing and search ------------------------------------------------------


def test_list_dir_renders_the_same_tree_as_the_other_providers(tmp_path) -> None:
    client = FakeAioClient(
        entries=[
            Entry(name="a.txt", path=f"{_USER_DATA}/workspace/a.txt", is_dir=False, size=1),
            Entry(
                name="sub",
                path=f"{_USER_DATA}/workspace/sub",
                is_dir=True,
                size=4096,
            ),
            Entry(
                name="b.txt",
                path=f"{_USER_DATA}/workspace/sub/b.txt",
                is_dir=False,
                size=2,
            ),
        ]
    )
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.list_dir(f"{_USER_DATA}/workspace") == [
        "├── sub/",
        "│   └── b.txt",
        "└── a.txt",
    ]


def test_list_dir_stops_at_max_depth(tmp_path) -> None:
    client = FakeAioClient(
        entries=[
            Entry(name="sub", path=f"{_USER_DATA}/sub", is_dir=True),
            Entry(name="deep.txt", path=f"{_USER_DATA}/sub/deep.txt", is_dir=False),
        ]
    )
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.list_dir(f"{_USER_DATA}", max_depth=1) == ["└── sub/"]


def test_list_dir_missing_is_file_not_found(tmp_path) -> None:
    client = FakeAioClient(list_error=FileNotFoundError("/mnt/user-data/missing"))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(FileNotFoundError):
        sandbox.list_dir(f"{_USER_DATA}/missing")


def test_list_dir_of_a_file_is_not_a_directory(tmp_path) -> None:
    client = FakeAioClient(list_error=NotADirectoryError("/mnt/user-data/a.txt"))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(NotADirectoryError):
        sandbox.list_dir(f"{_USER_DATA}/a.txt")


def test_empty_directory_returns_no_lines(tmp_path) -> None:
    sandbox = _sandbox(tmp_path, FakeAioClient(entries=[]))

    assert sandbox.list_dir(f"{_USER_DATA}/workspace") == []


def test_glob_and_grep_are_forwarded_with_their_flags(tmp_path) -> None:
    client = FakeAioClient(
        glob_result=([f"{_USER_DATA}/a.py"], True),
        grep_result=([GrepResult(path=f"{_USER_DATA}/a.py", line_number=2, line="needle")], False),
    )
    sandbox = _sandbox(tmp_path, client)

    assert sandbox.glob(_USER_DATA, "**/*.py", max_results=5) == ([f"{_USER_DATA}/a.py"], True)
    matches, truncated = sandbox.grep(
        _USER_DATA, "needle", glob="*.py", literal=True, case_sensitive=True, max_results=7
    )
    assert [(m.path, m.line_number, m.line) for m in matches] == [
        (f"{_USER_DATA}/a.py", 2, "needle")
    ]
    assert truncated is False


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
    sandbox = _namespaced(tmp_path, FakeAioClient(text="shared"))

    assert sandbox.read_file(f"{_PRIVATE_SKILLS}/a.txt") == "shared"


# -- transport failure -------------------------------------------------------


def test_a_transport_failure_notifies_the_owner_once(tmp_path) -> None:
    calls: list[str] = []
    client = FakeAioClient(command_error=AioTransportError("connection refused"))
    sandbox = _sandbox(tmp_path, client, on_transport_failure=lambda: calls.append("dead"))

    for _ in range(3):
        with pytest.raises(SandboxError):
            sandbox.execute_command("true")

    assert calls == ["dead"]


def test_a_failing_callback_does_not_hide_the_error(tmp_path) -> None:
    def explode() -> None:
        raise RuntimeError("callback exploded")

    client = FakeAioClient(command_error=AioTransportError("connection refused"))
    sandbox = _sandbox(tmp_path, client, on_transport_failure=explode)

    with pytest.raises(SandboxError):
        sandbox.execute_command("true")


def test_transport_failures_stay_oserror_for_files(tmp_path) -> None:
    client = FakeAioClient(text_error=AioTransportError("connection refused"))
    sandbox = _sandbox(tmp_path, client)

    with pytest.raises(OSError):
        sandbox.read_file(f"{_USER_DATA}/workspace/a.txt")
