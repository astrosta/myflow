"""MCP 配置的打码与回写（M7 §2.4）。

路由不碰磁盘：读、打码、还原客户端送回来的打码值、原子写都在这里；双锁和原子写本身
在 agentkit.config.extensions_config，路由里不出现 extensions_config_write_lock。

两条硬约束：

* 响应里没有原始凭据，也没有宿主机路径（env / headers / user_auth.users 全打成 ****）。
* 格式错误的凭据绝不能回显。detail 只说键名和允许集合，不带值，也不带被拒绝的命令行。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agentkit.config.extensions_config import (
    ExtensionsConfig,
    read_extensions_config_file,
    resolve_extensions_config_path,
    update_extensions_config,
)
from backend.app.gateway.errors import (
    InvalidExtensionsConfig,
    McpServerNotFound,
    StdioCommandRejected,
)
from backend.app.gateway.models.request import McpConfigUpdateRequest

logger = logging.getLogger(__name__)

MASK = "****"

#: 逐值打码的三块。headers_from_context 只存名字，不打码；PUT 整块替换。
_SECRET_MAPS = ("env", "headers")

#: oauth 里的凭据字段（§5.5 层 ①）。GET 时和 env/headers 一样打码。
_OAUTH_SECRET_KEYS = ("client_secret", "refresh_token")

_STDIO_ALLOWLIST_ENV_NAMES = ("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST",)

#: §5.7 第 1 条：API 注册 stdio 只接受允许列表里的**裸**可执行文件名。
DEFAULT_STDIO_COMMAND_ALLOWLIST = frozenset({"npx", "uvx"})


def stdio_command_allowlist() -> frozenset[str]:
    """默认 {npx, uvx}，环境变量可以扩（逗号分隔）。每次读，测试和热改都立刻生效。"""
    for name in _STDIO_ALLOWLIST_ENV_NAMES:
        raw = os.getenv(name)
        if raw:
            extra = {item.strip() for item in raw.split(",") if item.strip()}
            return DEFAULT_STDIO_COMMAND_ALLOWLIST | extra
    return DEFAULT_STDIO_COMMAND_ALLOWLIST


# --------------------------------------------------------------------------
# 打码
# --------------------------------------------------------------------------


def mask_mcp_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """GET 的形状：env / headers / user_auth.users 里每个非空字符串换成 ****，null 仍是 null。"""
    servers = raw.get("mcpServers")
    masked: dict[str, Any] = {}
    if isinstance(servers, Mapping):
        for name, server in servers.items():
            masked[str(name)] = _mask_server(server) if isinstance(server, Mapping) else server
    return {"mcpServers": masked}


def _mask_server(server: Mapping[str, Any]) -> dict[str, Any]:
    masked = dict(server)
    for key in _SECRET_MAPS:
        section = server.get(key)
        if isinstance(section, Mapping):
            masked[key] = {str(name): _mask_value(value) for name, value in section.items()}
    oauth = server.get("oauth")
    if isinstance(oauth, Mapping):
        masked_oauth = dict(oauth)
        for key in _OAUTH_SECRET_KEYS:
            if masked_oauth.get(key):
                masked_oauth[key] = MASK
        masked["oauth"] = masked_oauth
    user_auth = server.get("user_auth")
    if isinstance(user_auth, Mapping):
        users = user_auth.get("users")
        if isinstance(users, Mapping):
            masked_auth = dict(user_auth)
            masked_auth["users"] = {
                str(user): (
                    {str(key): _mask_value(value) for key, value in entry.items()}
                    if isinstance(entry, Mapping)
                    else entry
                )
                for user, entry in users.items()
            }
            masked["user_auth"] = masked_auth
    return masked


def _mask_value(value: Any) -> Any:
    return MASK if isinstance(value, str) and value else value


def read_extensions_config() -> dict[str, Any]:
    """GET 的读路径。显式/环境变量指向的文件中途消失 → FileNotFoundError，落到 500。

    日志里有路径，响应里没有（§2.4）。搜索模式没配 → 空配置，那是合法状态。
    """
    path = resolve_extensions_config_path()
    if path is None:
        return {}
    return read_extensions_config_file(path)


# --------------------------------------------------------------------------
# 打码回写
# --------------------------------------------------------------------------


def restore_masked_secrets(
    incoming: Mapping[str, Any], on_disk: Mapping[str, Any]
) -> dict[str, Any]:
    """把客户端原样送回的 **** 还原成磁盘上的原值（§2.4）。

    **** + 磁盘上没有这个键 → 422：打码串不能变成凭据。键被省略就是删除 ——
    PUT 是整份替换 mcpServers，不是 merge。
    """
    restored: dict[str, Any] = {}
    for name, server in incoming.items():
        if not isinstance(server, Mapping):
            restored[name] = server
            continue
        entry: dict[str, Any] = dict(server)
        stored = on_disk.get(name) if isinstance(on_disk, Mapping) else None
        stored_server = stored if isinstance(stored, Mapping) else {}
        for section in _SECRET_MAPS:
            if section in entry:
                entry[section] = _restore_secret_map(
                    entry[section], stored_server.get(section), where=f"{name}.{section}"
                )
        if "oauth" in entry:
            entry["oauth"] = _restore_secret_map(
                entry["oauth"], stored_server.get("oauth"), where=f"{name}.oauth"
            )
        if "user_auth" in entry:
            entry["user_auth"] = _restore_user_auth(
                entry["user_auth"],
                stored_server.get("user_auth"),
                where=f"{name}.user_auth",
            )
        restored[str(name)] = entry
    return restored


def _restore_secret_map(incoming: Any, stored: Any, *, where: str) -> Any:
    if not isinstance(incoming, Mapping):
        return incoming
    stored_map = stored if isinstance(stored, Mapping) else {}
    result = dict(incoming)
    for key, value in incoming.items():
        if value != MASK:
            continue
        if key not in stored_map:
            raise InvalidExtensionsConfig(
                f"{where}.{key} is masked but the stored config has no value to restore"
            )
        result[key] = stored_map[key]
    return result


def _restore_user_auth(incoming: Any, stored: Any, *, where: str) -> Any:
    if not isinstance(incoming, Mapping):
        return incoming
    result = dict(incoming)
    users = incoming.get("users")
    if isinstance(users, Mapping):
        stored_users = stored.get("users") if isinstance(stored, Mapping) else None
        stored_users = stored_users if isinstance(stored_users, Mapping) else {}
        result["users"] = {
            str(user): (
                _restore_secret_map(entry, stored_users.get(user), where=f"{where}.users.{user}")
                if isinstance(entry, Mapping)
                else entry
            )
            for user, entry in users.items()
        }
    return result


# --------------------------------------------------------------------------
# §5.7 启动校验（M7 只做第 1 条）
# --------------------------------------------------------------------------


def _transport_kind(server: Mapping[str, Any]) -> str:
    raw = server.get("type") or server.get("transport") or "stdio"
    return str(raw).strip().lower().replace("-", "_")


def check_stdio_commands(servers: Mapping[str, Any]) -> None:
    """启用中的 stdio 服务器，command 必须是允许列表里的裸可执行文件名。

    这是纵深防御，不是信任边界：npx / uvx 存在的意义就是拉取并执行远程包，
    真正的边界是管理员认证 + 网络可达性（§5.7）。远程 transport 不跑这一项。
    """
    allowed = stdio_command_allowlist()
    for name, server in servers.items():
        if not isinstance(server, Mapping):
            continue
        if not server.get("enabled", True):
            continue  # 已经坏掉的服务器也要能关掉
        if _transport_kind(server) != "stdio":
            continue
        command = server.get("command")
        if not isinstance(command, str) or command not in allowed:
            raise StdioCommandRejected(
                f"stdio command for MCP server {name} must be a bare executable name "
                f"from the allowed set: {', '.join(sorted(allowed))}"
            )


# --------------------------------------------------------------------------
# 读-改-写入口（锁和原子写都在 agentkit.config）
# --------------------------------------------------------------------------


def replace_mcp_servers(body: McpConfigUpdateRequest) -> dict[str, Any]:
    """PUT：整份替换 mcpServers，文件里的 skills / mcpInterceptors 一个都不动。"""
    incoming = {name: server.to_config_dict() for name, server in body.mcp_servers.items()}

    def mutate(data: dict[str, Any]) -> None:
        stored = data.get("mcpServers")
        restored = restore_masked_secrets(incoming, stored if isinstance(stored, Mapping) else {})
        check_stdio_commands(restored)
        _validate_mcp_servers(restored)
        data["mcpServers"] = restored

    return update_extensions_config(mutate)


def set_mcp_server_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """PATCH：只改一台的 enabled。找不到 → 404；启用 stdio 才跑允许列表。"""

    def mutate(data: dict[str, Any]) -> None:
        servers = data.get("mcpServers")
        if not isinstance(servers, Mapping) or name not in servers:
            raise McpServerNotFound(f"MCP server {name} not found")
        server = servers[name]
        if not isinstance(server, Mapping):
            raise InvalidExtensionsConfig(f"mcpServers.{name} must be an object")
        updated = dict(server)
        updated["enabled"] = enabled
        if enabled:
            check_stdio_commands({name: updated})
        data["mcpServers"] = {**servers, name: updated}

    return update_extensions_config(mutate)


def _validate_mcp_servers(servers: Mapping[str, Any]) -> None:
    """写入前过一遍模型。失败只报 loc + 错误类型 —— 值可能是凭据，绝不进 detail。"""
    try:
        ExtensionsConfig.model_validate({"mcpServers": dict(servers)})
    except ValidationError as exc:
        raise InvalidExtensionsConfig(_validation_reason(exc)) from None


def _validation_reason(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(item) for item in error.get('loc', ()))}: {error.get('type')}"
        for error in exc.errors()
    ]
    return "invalid mcpServers: " + ", ".join(parts)
