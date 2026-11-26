"""curl tool: one HTTP request inside the sandbox.

The model gets a small, explicit HTTP surface instead of building a shell command
from curl flags: URL, method, headers, body, redirects, and a timeout. The
command itself still runs through Sandbox.execute_command so every provider
(local / E2B / OpenSandbox) uses the same network and credential boundary as
bash. Structured fields plus shlex quoting keep user data out of the shell
grammar.

Only http and https URLs are accepted: this is an HTTP client, not a file reader
and not an SSH/SFTP client. The tool is hidden from the model when the local
provider has host bash disabled (see agentkit.tools.registry), because in that
deployment the sandbox cannot run shell commands at all.
"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.config.sandbox_config import DEFAULT_BASH_MAX_OUTPUT_CHARS
from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.lease import command_scope_from_context
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized, run_sync_tool_after_async_init
from agentkit.sandbox.tools.bash_tool import truncate_bash_output

TOOL_NAME = "curl"

_HTTP_SCHEMES = frozenset({"http", "https"})
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_\x60|~]+$")
_URL_WHITESPACE_RE = re.compile(r"\s")


@tool(TOOL_NAME)
async def curl_tool(
    url: str,
    runtime: ToolRuntime[Any, Any],
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
    include_headers: bool = False,
    description: str = "",
) -> str:
    """在沙箱中发起一次 HTTP 请求（curl）。

    适合调用 API、抓取网页或下载文本内容；只允许 http/https URL。
    请求结果返回给模型，超过 sandbox.bash_max_output_chars 时会保留头尾并标注截断。
    需要把响应落盘时，让 bash 用 curl -o /mnt/user-data/workspace/... 下载。

    Args:
        url: 完整的 http:// 或 https:// 地址。
        method: HTTP 方法，默认 GET，例如 POST / PUT / PATCH / DELETE。
        headers: 可选的请求头，例如 {"Authorization": "Bearer ..."}。
        body: 可选的请求体；配合 POST / PUT / PATCH 使用。
        timeout: 可选，墙钟超时（秒）；同时限制 curl 与沙箱命令。
        follow_redirects: 是否跟随 3xx 重定向，默认 true。
        include_headers: 是否把响应状态行和响应头一并返回，默认 false。
        description: 可选，界面上显示的短标签。
    """
    del description
    try:
        command = build_curl_command(
            url=url,
            method=method,
            headers=headers,
            body=body,
            timeout=timeout,
            follow_redirects=follow_redirects,
            include_headers=include_headers,
        )
        output = await run_sync_tool_after_async_init(_run, runtime, command, timeout)
        return truncate_bash_output(output, _max_output_chars(), label="curl")
    except SandboxError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {mask_host_paths(str(exc))}"


def build_curl_command(
    *,
    url: str,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
    include_headers: bool = False,
) -> str:
    """Build a shell-safe curl command from structured fields."""
    target = _validate_url(url)
    verb = _validate_method(method)
    timeout_value = _validate_timeout(timeout)
    header_args = _header_args(headers)

    if verb == "HEAD":
        # -X HEAD makes curl wait for a body the server never sends; --head is
        # curl's own HEAD mode and already prints the response headers.
        argv = ["curl", "-sS", "--fail-with-body", "--head"]
    else:
        argv = ["curl", "-sS", "--fail-with-body", "-X", verb]
    if follow_redirects:
        argv.append("-L")
    if include_headers and verb != "HEAD":
        argv.append("-i")
    if timeout_value is not None:
        argv.extend(["--max-time", _format_seconds(timeout_value)])
    argv.extend(header_args)
    if body is not None:
        payload = str(body)
        if payload.startswith("@"):
            # --data-binary treats a leading @ as a filename; a body is data, not
            # a request to read (and upload) a sandbox file.
            raise ValueError("body must not start with '@'")
        argv.extend(["--data-binary", payload])
    # End option parsing: a URL can never be mistaken for a curl flag.
    argv.extend(["--", target])
    return " ".join(shlex.quote(part) for part in argv)


def _run(runtime: ToolRuntime[Any, Any], command: str, timeout: float | None) -> str:
    """Sync body: runs in a worker thread, so it only touches the sandbox."""
    sandbox = ensure_sandbox_initialized(runtime)
    output = sandbox.execute_command_in_scope(
        command,
        timeout=timeout,
        # §11.2：每个子代理一个 command scope（独立 shell 会话）；lead 没有 scope。
        scope=command_scope_from_context(getattr(runtime, "context", None)),
    )
    return mask_host_paths(output, sandbox) or "(no output)"


def _validate_url(url: str) -> str:
    text = url.strip() if isinstance(url, str) else ""
    if not text or _URL_WHITESPACE_RE.search(text):
        raise ValueError("url must be a non-empty http:// or https:// URL without whitespace")
    parts = urlsplit(text)
    if parts.scheme.lower() not in _HTTP_SCHEMES or not parts.netloc:
        raise ValueError("url must be an absolute http:// or https:// URL")
    return text


def _validate_method(method: str) -> str:
    text = method.strip().upper() if isinstance(method, str) else ""
    if not text or not _METHOD_RE.fullmatch(text):
        raise ValueError("method must be an HTTP token such as GET, POST, PUT, PATCH, or DELETE")
    return text


def _validate_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    value = float(timeout)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    return value


def _header_args(headers: Mapping[str, str] | None) -> list[str]:
    if headers is None:
        return []
    if not isinstance(headers, Mapping):
        raise ValueError("headers must be a mapping of header names to values")

    args: list[str] = []
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip()
        value = str(raw_value)
        if not name or not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid header name: {raw_name!r}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"header {name!r} must not contain CR or LF")
        args.extend(["-H", f"{name}: {value}"])
    return args


def _format_seconds(value: float) -> str:
    return format(value, "g")


def _max_output_chars() -> int:
    """当前配置的上限；配置不可用时回退默认值，读取失败不能让 curl 工具失效。"""
    try:
        from agentkit.config import get_app_config

        configured = get_app_config().sandbox.bash_max_output_chars
        return max(1000, int(configured))
    except Exception:
        return DEFAULT_BASH_MAX_OUTPUT_CHARS


__all__ = ["TOOL_NAME", "build_curl_command", "curl_tool"]
