"""Generic run_code: execute a code snippet through the sandbox command channel.

Providers without a real code interpreter (local, cube, and opensandbox without
the code-interpreter image) share this path: write the snippet into the sandbox
workspace, run it with the matching interpreter, delete the temp file. It only
uses write_file / execute_command, so every provider gets it for free.
"""

from __future__ import annotations

import posixpath
import shlex
import uuid
from dataclasses import dataclass
from typing import Protocol

from agentkit.sandbox.exceptions import SandboxRuntimeError

#: Where the snippet lands inside the sandbox. It sits under the workspace so the
#: path guard already allows it; the directory is removed after the run.
RUN_CODE_DIR = "/mnt/user-data/workspace/.myflow-run"

_DEFAULT_TIMEOUT_SECONDS = 120.0
_CLEANUP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """How one language is executed through the shell."""

    interpreter: str
    extension: str


#: Languages the command channel can run. Everything else needs a real
#: interpreter (the OpenSandbox Code Interpreter), which is why it is absent
#: here instead of guessed at.
COMMAND_LANGUAGES: dict[str, LanguageSpec] = {
    "python": LanguageSpec("python3", "py"),
    "python3": LanguageSpec("python3", "py"),
    "javascript": LanguageSpec("node", "js"),
    "js": LanguageSpec("node", "js"),
    "node": LanguageSpec("node", "js"),
    "bash": LanguageSpec("bash", "sh"),
    "shell": LanguageSpec("bash", "sh"),
    "sh": LanguageSpec("bash", "sh"),
}

#: Languages only a code interpreter can run.
INTERPRETER_ONLY_LANGUAGES = frozenset({"typescript", "ts", "java", "go"})


class _SandboxLike(Protocol):
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    def execute_command(
        self, command: str, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> str: ...


def normalize_language(language: str) -> str:
    return (language or "").strip().lower() or "python"


def command_language(language: str) -> LanguageSpec:
    """Resolve one language for the command channel, or explain why it cannot."""

    normalized = normalize_language(language)
    spec = COMMAND_LANGUAGES.get(normalized)
    if spec is not None:
        return spec
    if normalized in INTERPRETER_ONLY_LANGUAGES:
        raise SandboxRuntimeError(
            f"language {normalized!r} needs a code interpreter; this sandbox only "
            "runs python, javascript and bash through its shell. Enable the "
            "OpenSandbox code interpreter (sandbox.opensandbox.run_code.image) or "
            "use one of the supported languages."
        )
    supported = ", ".join(sorted(COMMAND_LANGUAGES))
    raise SandboxRuntimeError(
        f"unsupported language {normalized!r}; supported here: {supported}"
    )


def run_code_via_command(
    sandbox: _SandboxLike,
    code: str,
    *,
    language: str = "python",
    timeout: float | None = None,
) -> str:
    """Write, run and clean up one snippet through execute_command."""

    spec = command_language(language)
    path = posixpath.join(RUN_CODE_DIR, f"snippet-{uuid.uuid4().hex[:12]}.{spec.extension}")
    limit = _DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    sandbox.write_file(path, code)
    try:
        command = f"{spec.interpreter} {shlex.quote(path)}"
        return sandbox.execute_command(command, timeout=limit)
    finally:
        _cleanup(sandbox, path)


def _cleanup(sandbox: _SandboxLike, path: str) -> None:
    """Best effort: a leftover temp file must not fail the run."""

    try:
        sandbox.execute_command(
            "rm -f -- " + shlex.quote(path), timeout=_CLEANUP_TIMEOUT_SECONDS
        )
    except Exception:
        return


__all__ = [
    "COMMAND_LANGUAGES",
    "INTERPRETER_ONLY_LANGUAGES",
    "RUN_CODE_DIR",
    "LanguageSpec",
    "command_language",
    "normalize_language",
    "run_code_via_command",
]
