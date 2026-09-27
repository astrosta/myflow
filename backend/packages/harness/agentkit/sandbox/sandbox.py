"""Sandbox ABC. The error contract in the class docstring is the provider boundary."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SIGPIPE = 13
_FIND_SUCCESS_CODES = frozenset({0, 128 + _SIGPIPE, -_SIGPIPE})


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str


def find_truncation_succeeded(find_status: int) -> bool:
    """Whether ``find`` itself succeeded when its output was cut with ``head``.

    ``sh -lc`` has no ``pipefail``, so the pipeline's ``$?`` is ``head``'s status.
    Callers must capture ``find``'s exit code on their own. ``head`` closing the
    pipe delivers SIGPIPE (exit 141); that truncation is success, not a failure.
    """
    return find_status in _FIND_SUCCESS_CODES


class Sandbox(ABC):
    """What a sandbox can do. Lifecycle lives on ``SandboxProvider``.

    Error contract (every provider must follow this; the tool layer depends on it):

    - ``list_dir`` missing path -> ``FileNotFoundError``
    - ``list_dir`` command or client failure -> ``OSError``
    - ``list_dir`` directory exists but is empty -> ``[]``
      Empty and failure must stay distinguishable. Returning ``[]`` on failure
      makes the tool render ``(empty)`` and the model reasons from a false premise.
    - ``download_file`` path escape -> ``PermissionError``
    - ``download_file`` unreadable or missing -> ``OSError``
    - ``execute_command`` illegal ``env`` key -> ``ValueError``

    ``resolve()`` and the later ``open()`` are not atomic (TOCTOU). Closing that
    window needs ``openat`` + ``O_NOFOLLOW``. M2 does not do that: a local sandbox
    is path normalization, not an isolation boundary. Remote providers supply the
    real boundary. This gap is intentional and known.

    Remote ``glob`` / ``grep`` that truncate with ``find | head`` must capture
    ``find``'s exit code explicitly. ``sh -lc`` has no ``pipefail`` by default, so
    ``$?`` is ``head``'s status. SIGPIPE caused by truncation counts as success.
    """

    _id: str

    #: Whether ``execute_command`` reuses a persistent shell (exports, cwd, functions).
    #: Three-state and fail closed: ``None`` means the implementation did not declare.
    #: A third-party provider might reuse a session, so silence is not a clean shell.
    #: Consumers trust only an explicit ``False``; treat ``None`` like ``True``.
    persistent_shell_sessions: bool | None = None

    @property
    def id(self) -> str:
        return self._id

    @staticmethod
    def validate_env_keys(env: dict[str, str] | None) -> None:
        """Reject env keys that are not POSIX names. Abstract-layer defense."""
        if not env:
            return
        for key in env:
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"Illegal environment variable name: {key!r}")

    def execute_command_in_scope(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        *,
        scope: str | None = None,
    ) -> str:
        """Default pass-through. M8 scopes isolate concurrent sub-agent shells."""
        del scope
        return self.execute_command(command, env=env, timeout=timeout)

    def execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a command. ``env`` is per call so M9 can pass a short-lived token.

        Illegal ``env`` keys raise ``ValueError`` before the provider runs anything.
        Implementations override ``_execute_command`` and do not re-check the keys.
        """
        self.validate_env_keys(env)
        return self._execute_command(command, env=env, timeout=timeout)

    @abstractmethod
    def _execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Provider body of ``execute_command``. ``env`` keys are already validated."""

    @abstractmethod
    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read text. ``start_line`` and ``end_line`` are 1-indexed and inclusive.

        Text and bytes stay separate: a remote provider may serve text over a
        shell and bytes over a file API. One method would force a type branch.
        """

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """Write text. ``append`` adds to the file; the default overwrites it."""

    @abstractmethod
    def download_file(self, path: str) -> bytes:
        """Read bytes.

        Path escape raises ``PermissionError``. A missing or unreadable file raises
        ``OSError``, including when the provider's native error was ``FileNotFoundError``.
        """

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None:
        """Write bytes. This is not ``write_file``: the transport is different."""

    @abstractmethod
    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """List a directory. Empty and failure are different results.

        - Missing path raises ``FileNotFoundError``.
        - A command or client failure raises ``OSError``.
        - A directory that exists and is empty returns ``[]``.

        Returning ``[]`` on failure makes the tool render ``(empty)``.
        """

    @abstractmethod
    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        """Find paths. Return ``(matches, truncated)`` so the tool can say there is more."""

    @abstractmethod
    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        """Search file contents. Return ``(matches, truncated)``; do not drop the flag."""
