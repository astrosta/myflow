"""File tools. Sandbox errors become strings so the agent loop can continue."""

from __future__ import annotations

import hashlib

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.file_operation_lock import get_file_operation_lock
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized
from agentkit.sandbox.sandbox import Sandbox

# M6 installs read-before-write here. M2 only reserves the call sites.


def note_file_read(sandbox: Sandbox, path: str, content_hash: str) -> None:
    del sandbox, path, content_hash


def assert_may_write(sandbox: Sandbox, path: str, content_hash: str) -> None:
    del sandbox, path, content_hash


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fail(exc: Exception, runtime: ToolRuntime) -> str:
    del runtime
    if isinstance(exc, SandboxError):
        return f"Error: {exc}"
    return f"Error: {mask_host_paths(str(exc))}"


@tool
def read_file(
    path: str,
    runtime: ToolRuntime,
    start_line: int | None = None,
    end_line: int | None = None,
    description: str = "",
) -> str:
    """Read a text file from the sandbox.

    Args:
        path: Absolute virtual path, e.g. /mnt/user-data/workspace/main.py
        start_line: 1-indexed first line to read (inclusive).
        end_line: 1-indexed last line to read (inclusive).
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        content = sandbox.read_file(path, start_line=start_line, end_line=end_line)
        note_file_read(sandbox, path, _content_hash(content))
        return mask_host_paths(content, sandbox)
    except Exception as exc:
        return _fail(exc, runtime)


@tool
def write_file(
    path: str,
    content: str,
    runtime: ToolRuntime,
    append: bool = False,
    description: str = "",
) -> str:
    """Write a text file in the sandbox. Parent directories are created.

    Args:
        path: Absolute virtual path.
        content: Text to write.
        append: When true, append instead of overwriting.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        assert_may_write(sandbox, path, _content_hash(content))
        lock = get_file_operation_lock(sandbox, path)
        with lock:
            sandbox.write_file(path, content, append=append)
        return f"Wrote {path}"
    except Exception as exc:
        return _fail(exc, runtime)


@tool
def str_replace(
    path: str,
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
    description: str = "",
) -> str:
    """Replace one unique substring in a sandbox file.

    Args:
        path: Absolute virtual path.
        old_string: Exact text to replace. Must match exactly once.
        new_string: Replacement text.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        lock = get_file_operation_lock(sandbox, path)
        with lock:
            content = sandbox.read_file(path)
            count = content.count(old_string)
            if count == 0:
                return "Error: old_string was not found. Read the file and include more context."
            if count > 1:
                return (
                    f"Error: old_string matched {count} times. "
                    "Provide more surrounding context so the match is unique."
                )
            updated = content.replace(old_string, new_string, 1)
            assert_may_write(sandbox, path, _content_hash(updated))
            sandbox.write_file(path, updated, append=False)
        return f"Updated {path}"
    except Exception as exc:
        return _fail(exc, runtime)


@tool
def ls(path: str, runtime: ToolRuntime, description: str = "") -> str:
    """List a sandbox directory as a tree, at most two levels deep.

    Args:
        path: Absolute virtual path.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        lines = sandbox.list_dir(path, max_depth=2)
        if not lines:
            return "(empty)"
        return mask_host_paths("\n".join(lines), sandbox)
    except Exception as exc:
        return _fail(exc, runtime)


@tool
def glob(
    path: str,
    pattern: str,
    runtime: ToolRuntime,
    include_dirs: bool = False,
    description: str = "",
) -> str:
    """Find files under a sandbox directory.

    Args:
        path: Absolute virtual directory.
        pattern: Glob pattern, for example ``**/*.py``.
        include_dirs: Include directories in the results.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        matches, truncated = sandbox.glob(path, pattern, include_dirs=include_dirs)
        body = "\n".join(matches) if matches else "(empty)"
        if truncated:
            body += "\n\n[truncated: narrow the pattern to see more]"
        return mask_host_paths(body, sandbox)
    except Exception as exc:
        return _fail(exc, runtime)


@tool
def grep(
    path: str,
    pattern: str,
    runtime: ToolRuntime,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    description: str = "",
) -> str:
    """Search file contents under a sandbox path.

    Args:
        path: Absolute virtual file or directory.
        pattern: Regular expression, or a literal string when ``literal`` is true.
        glob: Optional filename glob.
        literal: Treat ``pattern`` as plain text.
        case_sensitive: Match case when true.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        matches, truncated = sandbox.grep(
            path,
            pattern,
            glob=glob,
            literal=literal,
            case_sensitive=case_sensitive,
        )
        if not matches:
            body = "(empty)"
        else:
            body = "\n".join(f"{item.path}:{item.line_number}:{item.line}" for item in matches)
        if truncated:
            body += "\n\n[truncated: narrow the pattern to see more]"
        return mask_host_paths(body, sandbox)
    except Exception as exc:
        return _fail(exc, runtime)
