"""LocalSandbox.run_code: needs the same trust switch as the bash tool."""

from __future__ import annotations

import pytest

from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.local.local_sandbox import LocalSandbox
from agentkit.sandbox.security import PathMapping


def _sandbox(tmp_path) -> LocalSandbox:
    (tmp_path / "user-data" / "workspace").mkdir(parents=True, exist_ok=True)
    return LocalSandbox(
        "local",
        [PathMapping("/mnt/user-data", str(tmp_path / "user-data"))],
        command_timeout=60,
    )


def test_run_code_is_refused_without_host_bash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox.is_host_bash_allowed", lambda: False
    )

    with pytest.raises(SandboxRuntimeError, match="allow_host_bash"):
        _sandbox(tmp_path).run_code("print(1)")


def test_run_code_reaches_the_command_channel_when_trusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agentkit.sandbox.local.local_sandbox.is_host_bash_allowed", lambda: True
    )
    sandbox = _sandbox(tmp_path)
    seen: list[str] = []

    def fake_execute(command: str, env=None, timeout=None) -> str:
        seen.append(command)
        return "42\n"

    monkeypatch.setattr(sandbox, "execute_command", fake_execute)

    assert sandbox.run_code("print(6 * 7)") == "42\n"
    assert seen[0].startswith("python3 ")
    assert seen[-1].startswith("rm -f -- ")
