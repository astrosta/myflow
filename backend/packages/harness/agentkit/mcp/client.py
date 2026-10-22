"""配置 → 适配器连接参数，以及 stdio 的启动前校验（M7 §5.2/§5.7）。

两个来源共用这一段的纪律：M7 里 stdio 参数只来自**运维文件**，后续阶段加上注册 API
之后会多一个**完全不可信**的来源——所以校验挂在"准备 spawn 之前"，不是挂在某个路由上。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentkit.config.app_config import AppConfig
from agentkit.config.extensions_config import ExtensionsConfig, McpServerConfig
from agentkit.config.paths import get_paths
from agentkit.config.runtime_paths import runtime_home
from agentkit.mcp.headers import (
    apply_header_overrides,
    duplicate_header_spellings,
    illegal_header_value_reason,
)

logger = logging.getLogger(__name__)

#: 过程产物目录：worker 的 cwd 与临时目录都落在 thread workspace 下（§5.3）。
MCP_TMP_DIRNAME = ".mcp"
MCP_TMP_SUBDIR = "tmp"

DEFAULT_STDIO_COMMAND_ALLOWLIST = frozenset({"npx", "uvx"})
STDIO_COMMAND_ALLOWLIST_ENV_NAMES = ("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST",)

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_SSE_READ_TIMEOUT_SECONDS = 300.0

#: adapter 只认这几个 transport 名。
_ADAPTER_TRANSPORTS = {"stdio": "stdio", "sse": "sse", "http": "http", "streamable_http": "http"}


class McpConfigRejected(ValueError):
    """这一台服务器的静态配置不可用：只跳过它，不拖垮整个 make_lead_agent（§5.2）。"""


def stdio_command_allowlist() -> frozenset[str]:
    """默认 {npx, uvx}，环境变量可以扩（逗号分隔）。每次读，热改立刻生效。"""
    for name in STDIO_COMMAND_ALLOWLIST_ENV_NAMES:
        raw = os.getenv(name)
        if raw:
            extra = {item.strip() for item in raw.split(",") if item.strip()}
            return DEFAULT_STDIO_COMMAND_ALLOWLIST | extra
    return DEFAULT_STDIO_COMMAND_ALLOWLIST


def validate_stdio_command(
    command: str | None, *, allowlist: frozenset[str] | None = None
) -> str | None:
    """返回拒绝原因，None = 通过。**这是纵深防御，不是信任边界**（§5.7）。

    npx / uvx 存在的意义就是拉取并执行远程包；真正的边界是管理员认证 + 网络可达性。
    """
    allowed = allowlist if allowlist is not None else stdio_command_allowlist()
    if not command or command != command.strip():
        return "command must be a bare executable name"
    if any(sep in command for sep in ("/", "\\")) or any(ch.isspace() for ch in command):
        return "command must not contain path separators or whitespace"
    if command not in allowed:
        return f"command must be one of: {', '.join(sorted(allowed))}"
    return None


def enabled_servers(config: ExtensionsConfig) -> dict[str, McpServerConfig]:
    return {name: server for name, server in config.mcp_servers.items() if server.enabled}


def workspace_dir(thread_id: str, *, user_id: str | None = None) -> Path:
    return get_paths().sandbox_work_dir(thread_id, user_id=user_id)


def discovery_dir(server_name: str) -> Path:
    """发现阶段用的临时 cwd。真正的调用走 (user:thread) 的会话池，cwd 是那个 thread 的 workspace。"""
    path = runtime_home() / MCP_TMP_DIRNAME / "discovery" / server_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_stdio_dirs(thread_id: str, *, user_id: str | None = None) -> tuple[Path, Path]:
    """(cwd, tmp)。stdio 服务器是有状态子进程，cwd 与临时目录都钉在 thread workspace 下。"""
    workspace = workspace_dir(thread_id, user_id=user_id)
    tmp = workspace / MCP_TMP_DIRNAME / MCP_TMP_SUBDIR
    tmp.mkdir(parents=True, exist_ok=True)
    return workspace, tmp


def build_stdio_env(server: McpServerConfig, *, tmp_dir: Path) -> dict[str, str]:
    """env 的值走 M0 的 $ENV 展开（只展开值，不展开键）。TMPDIR 之类默认指到 .mcp/tmp。"""
    expanded = AppConfig.resolve_env_variables(dict(server.env or {}))
    env = {str(key): str(value) for key, value in expanded.items()}
    for name in ("TMPDIR", "TMP", "TEMP"):
        env.setdefault(name, str(tmp_dir))  # 运维显式配了就用运维的
    return env


def validate_static_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """静态 headers：两种拼写直接拒；值非法也拒（**不回显值**，§5.6）。"""
    duplicates = duplicate_header_spellings(headers)
    if duplicates:
        raise McpConfigRejected(
            f"static headers repeat the same field with different spellings: {', '.join(duplicates)}"
        )
    cleaned: dict[str, str] = {}
    for key, value in (headers or {}).items():
        reason = illegal_header_value_reason(str(value))
        if reason is not None:
            raise McpConfigRejected(f"static header {key} is illegal: {reason}")
        cleaned[str(key)] = str(value)
    return cleaned


def build_server_params(
    server: McpServerConfig,
    *,
    server_name: str,
    user_id: str | None = None,
    thread_id: str | None = None,
    scratch: bool = False,
) -> dict[str, Any]:
    """把配置翻译成适配器的 server params。凭据在这一步只做**渲染**，合法性由 headers.py 判。"""
    kind = _ADAPTER_TRANSPORTS.get(server.transport_kind)
    if kind is None:
        raise McpConfigRejected(f"unsupported transport: {server.type!r}")
    if kind == "stdio":
        if scratch or not thread_id:
            workspace = discovery_dir(server_name)
            tmp = workspace / MCP_TMP_SUBDIR
            tmp.mkdir(parents=True, exist_ok=True)
        else:
            workspace, tmp = prepare_stdio_dirs(thread_id, user_id=user_id)
        return {
            "transport": "stdio",
            "command": server.command,
            "args": [str(item) for item in server.args],
            "env": build_stdio_env(server, tmp_dir=tmp),
            "cwd": str(workspace),
        }
    if not server.url:
        raise McpConfigRejected(f"{kind} server needs a url")
    params: dict[str, Any] = {
        "transport": kind,
        "url": server.url,
        "headers": apply_header_overrides(validate_static_headers(server.headers), {}),
    }
    if server.tool_call_timeout is not None:
        params["timeout"] = float(server.tool_call_timeout)
    params["sse_read_timeout"] = DEFAULT_SSE_READ_TIMEOUT_SECONDS
    return params


def build_servers_config(
    config: ExtensionsConfig,
    *,
    user_id: str | None = None,
    thread_id: str | None = None,
    scratch: bool = False,
) -> dict[str, dict[str, Any]]:
    """可用的服务器 → 连接参数。单台失败：日志 + 跳过，其它服务器照常（§5.2）。"""
    servers: dict[str, dict[str, Any]] = {}
    for name, server in enabled_servers(config).items():
        if server.transport_kind == "stdio":
            reason = validate_stdio_command(server.command)
            if reason is not None:
                # 运维文件里写了非法 command → 跳过那一台并记原因，不让整个 make_lead_agent 失败
                logger.warning("Skipping MCP server %s: %s (§5.7)", name, reason)
                continue
        try:
            servers[name] = build_server_params(
                server, server_name=name, user_id=user_id, thread_id=thread_id, scratch=scratch
            )
        except McpConfigRejected as exc:
            logger.warning("Skipping MCP server %s: %s", name, exc)
    return servers
