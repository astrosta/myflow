"""AIO data-plane client: one bash session per command, structured env, bytes.

The client is the contract boundary between our Sandbox methods and
agent-sandbox, so these tests pin the SDK call shape and the error mapping.
"""

from __future__ import annotations

import pytest

from agentkit.sandbox.aio_sandbox.client import SdkAioClient
from agentkit.sandbox.exceptions import SandboxRuntimeError, SandboxTimeoutError
from fakes.aio_sdk import ApiError, FakeAioSdk, FakeBashExec, FakeFileEntry


def _client(fake: FakeAioSdk, monkeypatch) -> SdkAioClient:
    client = SdkAioClient("http://127.0.0.1:41000", timeout=5)
    monkeypatch.setattr(client, "_sdk_client", lambda: fake)
    return client


def test_command_runs_in_its_own_session_and_closes_it(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_result = FakeBashExec(stdout="hi\n", stderr="", exit_code=0)

    client = _client(fake, monkeypatch)
    output = client.command("echo hi", timeout=30, max_output_bytes=1024)

    assert (output.text, output.exit_code) == ("hi", 0)
    created = fake.calls_of("bash.create_session")
    executed = fake.calls_of("bash.exec")
    closed = fake.calls_of("bash.close_session")
    assert len(created) == 1 and len(executed) == 1 and len(closed) == 1
    session_id = created[0]["session_id"]
    assert executed[0]["session_id"] == session_id
    assert closed[0]["session_id"] == session_id
    assert fake.sessions[session_id].closed is True


def test_each_command_gets_a_fresh_session(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.command("true", timeout=30, max_output_bytes=1024)
    client.command("true", timeout=30, max_output_bytes=1024)

    sessions = [call["session_id"] for call in fake.calls_of("bash.create_session")]
    assert len(sessions) == 2
    assert sessions[0] != sessions[1]


def test_command_passes_env_structurally_and_not_in_the_command(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.command(
        "printenv MYFLOW_TOKEN",
        env={"MYFLOW_TOKEN": "s3cret"},
        timeout=30,
        max_output_bytes=1024,
    )

    executed = fake.calls_of("bash.exec")[0]
    assert executed["env"] == {"MYFLOW_TOKEN": "s3cret"}
    assert "s3cret" not in executed["command"]
    assert executed["hard_timeout"] == 30
    assert executed["max_output_length"] == 1024
    # The request must outlive the server-side kill so the answer is not lost.
    assert executed["request_options"]["timeout_in_seconds"] > 30


def test_command_merges_stderr_and_keeps_the_exit_code(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_result = FakeBashExec(stdout="out", stderr="warn", exit_code=2)
    client = _client(fake, monkeypatch)

    output = client.command("false", timeout=30, max_output_bytes=1024)

    assert output.exit_code == 2
    assert output.text == "out\nwarn"


def test_command_timeout_raises_the_timeout_error(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_result = FakeBashExec(status="timed_out", exit_code=None)
    client = _client(fake, monkeypatch)

    with pytest.raises(SandboxTimeoutError):
        client.command("sleep 999", timeout=5, max_output_bytes=1024)


@pytest.mark.parametrize("status", ["killed", "running", "pending", "weird"])
def test_non_success_statuses_raise_runtime_errors(status, monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_result = FakeBashExec(status=status, exit_code=None)
    client = _client(fake, monkeypatch)

    with pytest.raises(SandboxRuntimeError):
        client.command("true", timeout=5, max_output_bytes=1024)


def test_a_missing_bash_namespace_names_the_image_problem(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_unsupported = True
    client = _client(fake, monkeypatch)

    with pytest.raises(SandboxRuntimeError) as exc_info:
        client.command("true", timeout=5, max_output_bytes=1024)

    message = str(exc_info.value)
    assert "1.9.3" in message
    assert "sandbox.aio.image" in message
    # No fallback transport is attempted: the shell namespace is never touched.
    assert fake.calls_of("shell.exec_command") == []


def test_a_transport_error_becomes_a_runtime_error(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.bash_error = RuntimeError("connection reset")
    client = _client(fake, monkeypatch)

    with pytest.raises(SandboxRuntimeError, match="connection reset"):
        client.command("true", timeout=5, max_output_bytes=1024)


def test_read_text_maps_line_ranges_to_the_zero_based_api(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.files["/mnt/user-data/workspace/a.txt"] = "one\ntwo\nthree\n"
    client = _client(fake, monkeypatch)

    assert client.read_text("/mnt/user-data/workspace/a.txt", start_line=2, end_line=3) == (
        "one\ntwo\nthree\n"
    )
    call = fake.calls_of("file.read_file")[0]
    assert call["file"] == "/mnt/user-data/workspace/a.txt"
    assert call["start_line"] == 1
    assert call["end_line"] == 3


def test_read_text_of_a_missing_file_is_file_not_found(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    with pytest.raises(FileNotFoundError):
        client.read_text("/mnt/user-data/workspace/missing.txt")


def test_write_text_passes_append_and_forwards_errors(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.write_text("/a.txt", "one", append=False)
    client.write_text("/a.txt", "two", append=True)

    assert fake.files["/a.txt"] == "onetwo"
    assert fake.calls_of("file.write_file")[1]["append"] is True

    fake.file_error = ApiError(403)
    with pytest.raises(SandboxRuntimeError):
        client.write_text("/a.txt", "x")


def test_read_bytes_streams_and_enforces_the_cap(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.files["/a.bin"] = "0123456789"
    fake.download_chunks = 4
    client = _client(fake, monkeypatch)

    assert client.read_bytes("/a.bin", max_bytes=100) == b"0123456789"

    with pytest.raises(OSError, match="transfer limit"):
        client.read_bytes("/a.bin", max_bytes=4)


def test_write_bytes_uploads_raw_bytes(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.write_bytes("/a.bin", b"\x00\x01")

    call = fake.calls_of("file.upload_file")[0]
    assert call["file"] == b"\x00\x01"
    assert call["path"] == "/a.bin"


def test_list_entries_maps_file_info(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.listings["/mnt/user-data"] = [
        FakeFileEntry(name="workspace", path="/mnt/user-data/workspace", is_directory=True, size=4096),
        FakeFileEntry(name="a.txt", path="/mnt/user-data/a.txt", is_directory=False, size=12),
    ]
    client = _client(fake, monkeypatch)

    entries = client.list_entries("/mnt/user-data", max_depth=1)

    assert [(entry.name, entry.is_dir, entry.size) for entry in entries] == [
        ("workspace", True, 4096),
        ("a.txt", False, 12),
    ]
    assert fake.calls_of("file.list_path")[0]["max_depth"] == 1


def test_list_entries_on_a_file_is_not_a_directory(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.listings["/mnt/user-data/a.txt"] = [
        FakeFileEntry(name="a.txt", path="/mnt/user-data/a.txt", is_directory=False)
    ]
    client = _client(fake, monkeypatch)

    with pytest.raises(NotADirectoryError):
        client.list_entries("/mnt/user-data/a.txt", max_depth=1)


def test_list_entries_of_a_missing_path_is_file_not_found(monkeypatch) -> None:
    client = _client(FakeAioSdk(), monkeypatch)

    with pytest.raises(FileNotFoundError):
        client.list_entries("/mnt/user-data/missing", max_depth=1)


def test_glob_uses_the_native_api_and_reports_truncation(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.glob_matches = [
        FakeFileEntry(name="a.py", path="/root/a.py"),
        FakeFileEntry(name="b.py", path="/root/b.py"),
    ]
    fake.glob_truncated = True
    client = _client(fake, monkeypatch)

    matches, truncated = client.glob("/root", "**/*.py", max_results=2)

    assert matches == ["/root/a.py", "/root/b.py"]
    assert truncated is True
    call = fake.calls_of("file.glob_files")[0]
    assert call["pattern"] == "**/*.py"
    assert call["files_only"] is True
    assert call["max_results"] == 2


def test_glob_can_include_directories(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.glob("/root", "**", include_dirs=True)

    assert fake.calls_of("file.glob_files")[0]["files_only"] is False


def test_grep_maps_matches_and_search_flags(monkeypatch) -> None:
    fake = FakeAioSdk()
    fake.grep_matches = [
        {"file": "/root/a.py", "line_number": 3, "line_content": "needle"},
    ]
    fake.grep_truncated = True
    client = _client(fake, monkeypatch)

    matches, truncated = client.grep(
        "/root", "needle", glob="*.py", literal=True, case_sensitive=False
    )

    assert [(m.path, m.line_number, m.line) for m in matches] == [("/root/a.py", 3, "needle")]
    assert truncated is True
    call = fake.calls_of("file.grep_files")[0]
    assert call["fixed_strings"] is True
    assert call["case_insensitive"] is True
    assert call["include"] == ["*.py"]


def test_grep_without_a_glob_filter_omits_include(monkeypatch) -> None:
    fake = FakeAioSdk()
    client = _client(fake, monkeypatch)

    client.grep("/root", "x")

    assert "include" not in fake.calls_of("file.grep_files")[0]
    assert fake.calls_of("file.grep_files")[0]["case_insensitive"] is True
