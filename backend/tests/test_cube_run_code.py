"""CubeSandbox.run_code: the snippet goes through the shell channel."""

from __future__ import annotations

from agentkit.sandbox.cube.mounts import MountEntry
from agentkit.sandbox.cube.sandbox import CubeSandbox
from fakes.cube_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox

_USER_DATA = "/mnt/user-data"


def _sandbox(tmp_path) -> CubeSandbox:
    sdk = FakeSdkSandbox(
        files=FakeFiles({_USER_DATA: tmp_path / "user-data"}),
        commands=FakeCommands(),
    )
    mounts = [MountEntry(_USER_DATA, str(tmp_path / "user-data"), False)]
    return CubeSandbox(sdk, sandbox_id="cube:s1", mounts=mounts, command_timeout=600)


def test_run_code_writes_runs_and_removes_the_snippet(tmp_path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult("42\n")

    assert sandbox.run_code("print(6 * 7)") == "42\n"

    commands = [call["cmd"] for call in sandbox._sdk.commands.calls]
    run = next(cmd for cmd in commands if "python3 " in cmd)
    assert ".myflow-run/snippet-" in run
    assert ".py" in run
    # The commands are wrapped by the output cap, so match on containment.
    assert any("rm -f -- " in cmd for cmd in commands)
    written = tmp_path / "user-data" / "workspace" / ".myflow-run"
    assert written.is_dir()  # the fake command handler does not really delete


def test_run_code_declares_support(tmp_path) -> None:
    assert _sandbox(tmp_path).supports_run_code is True


def test_run_code_rejects_typescript_without_an_interpreter(tmp_path) -> None:
    import pytest

    from agentkit.sandbox.exceptions import SandboxRuntimeError

    sandbox = _sandbox(tmp_path)

    with pytest.raises(SandboxRuntimeError, match="code interpreter"):
        sandbox.run_code("let x = 1", language="typescript")
