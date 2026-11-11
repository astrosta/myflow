"""File tools. Sandbox errors become strings so the agent loop can continue."""

from __future__ import annotations

import hashlib

from typing import Any
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
    runtime: ToolRuntime[Any, Any],
    start_line: int | None = None,
    end_line: int | None = None,
    description: str = "",
) -> str:
    """读取沙箱中的文本文件。

    Args:
        path: 绝对虚拟路径，例如 /mnt/user-data/workspace/main.py
        start_line: 起始行号（从 1 开始，包含）。
        end_line: 结束行号（从 1 开始，包含）。
        description: 可选，界面上显示的短标签。
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
    runtime: ToolRuntime[Any, Any],
    append: bool = False,
    description: str = "",
) -> str:
    """在沙箱中写入文本文件，父目录会自动创建。

    需要交付给用户下载或打开的最终文件请写到
    /mnt/user-data/outputs/；workspace 只用于临时文件。

    Args:
        path: 绝对虚拟路径。
        content: 要写入的文本。
        append: 为 true 时追加写入，而不是覆盖。
        description: 可选，界面上显示的短标签。
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
    runtime: ToolRuntime[Any, Any],
    description: str = "",
) -> str:
    """替换沙箱文件中一个唯一的子串。

    Args:
        path: 绝对虚拟路径。
        old_string: 要替换的原文，必须恰好匹配一次。
        new_string: 替换后的文本。
        description: 可选，界面上显示的短标签。
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
def ls(path: str, runtime: ToolRuntime[Any, Any], description: str = "") -> str:
    """以树形列出沙箱目录，最多两层。

    Args:
        path: 绝对虚拟路径。
        description: 可选，界面上显示的短标签。
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
    runtime: ToolRuntime[Any, Any],
    include_dirs: bool = False,
    description: str = "",
) -> str:
    """在沙箱目录下查找文件。

    Args:
        path: 绝对虚拟目录。
        pattern: Glob 模式，例如 ``**/*.py``。
        include_dirs: 是否在结果中包含目录。
        description: 可选，界面上显示的短标签。
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
    runtime: ToolRuntime[Any, Any],
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    description: str = "",
) -> str:
    """在沙箱路径下搜索文件内容。

    Args:
        path: 绝对虚拟文件或目录。
        pattern: 正则表达式；``literal`` 为 true 时按普通字符串处理。
        glob: 可选的文件名 glob。
        literal: 把 ``pattern`` 当作普通文本。
        case_sensitive: 为 true 时区分大小写。
        description: 可选，界面上显示的短标签。
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
