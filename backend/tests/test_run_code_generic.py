"""Generic run_code channel: interpreter mapping, temp file, cleanup."""

from __future__ import annotations

import pytest

from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.run_code import (
    COMMAND_LANGUAGES,
    RUN_CODE_DIR,
    command_language,
    run_code_via_command,
)


class FakeSandbox:
    def __init__(self, output: str = "ok\n") -> None:
        self.output = output
        self.writes: list[dict] = []
        self.commands: list[dict] = []

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.writes.append({"path": path, "content": content, "append": append})

    def execute_command(self, command: str, env=None, timeout=None) -> str:
        self.commands.append({"command": command, "timeout": timeout})
        return self.output


def test_python_lands_in_the_workspace_and_is_removed() -> None:
    sandbox = FakeSandbox("42\n")

    output = run_code_via_command(sandbox, "print(6 * 7)")

    assert output == "42\n"
    write = sandbox.writes[0]
    assert write["path"].startswith(RUN_CODE_DIR + "/snippet-")
    assert write["path"].endswith(".py")
    assert write["content"] == "print(6 * 7)"
    assert sandbox.commands[0]["command"] == "python3 " + write["path"]
    assert sandbox.commands[0]["timeout"] == 120.0
    # The temp file is deleted even though nothing failed.
    assert sandbox.commands[-1]["command"].startswith("rm -f -- ")
    assert write["path"] in sandbox.commands[-1]["command"]


def test_language_selects_the_interpreter_and_extension() -> None:
    for language, interpreter, extension in (
        ("python", "python3", "py"),
        ("javascript", "node", "js"),
        ("js", "node", "js"),
        ("bash", "bash", "sh"),
    ):
        sandbox = FakeSandbox()
        run_code_via_command(sandbox, "x", language=language)
        assert sandbox.writes[0]["path"].endswith("." + extension)
        assert sandbox.commands[0]["command"].startswith(interpreter + " ")


def test_timeout_is_forwarded() -> None:
    sandbox = FakeSandbox()

    run_code_via_command(sandbox, "x", timeout=7)

    assert sandbox.commands[0]["timeout"] == 7


def test_interpreter_only_languages_fail_with_an_actionable_message() -> None:
    for language in ("typescript", "java", "go"):
        with pytest.raises(SandboxRuntimeError, match="code interpreter"):
            command_language(language)


def test_unknown_language_lists_what_is_supported() -> None:
    with pytest.raises(SandboxRuntimeError, match="unsupported language"):
        command_language("cobol")


def test_cleanup_failure_does_not_mask_the_result() -> None:
    class BrokenCleanup(FakeSandbox):
        def execute_command(self, command: str, env=None, timeout=None) -> str:
            self.commands.append({"command": command, "timeout": timeout})
            if command.startswith("rm -f"):
                raise RuntimeError("rm exploded")
            return self.output

    sandbox = BrokenCleanup("done\n")

    assert run_code_via_command(sandbox, "x") == "done\n"


def test_command_languages_cover_python_js_and_bash() -> None:
    assert set(COMMAND_LANGUAGES) >= {"python", "javascript", "bash"}
