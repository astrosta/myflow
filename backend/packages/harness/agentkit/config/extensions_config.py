"""extensions_config.json 的读写地基（M7 §2.1–§2.3）。

MCP 服务器列表和技能的启用状态都存在这一个 JSON 里，它**同时被 Gateway API 和运行时
读写**——用户在网页上点一下"启用某技能"就是一次读-改-写。所以：

* 所有读-改-写走 update_extensions_config()：双锁（进程内 threading.Lock +
  跨进程文件咨询锁）覆盖 from_file → merge → atomic_write → reload 整个循环（§2.2）。
* 落盘走 atomic_write_extensions_config()：同目录临时文件 + fsync + os.replace()。
  只有 Docker 单文件挂载的 EBUSY 才退化为就地覆写（那条路径不原子），其它 errno 一律抛。
* 缓存失效比"解析后的路径 + 内容签名"，不做 mtime 大小比较（§2.3）。

JSON **不允许**列 Python 扩展路径去 import（那是 config.yaml 的 plugins:）；
mcpInterceptors 会 import 代码，是运维文件专属，Gateway API 不提供写入它的接口。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.config.atomic_json import atomic_write_json, read_json_file
from agentkit.config.file_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, advisory_file_lock
from agentkit.config.file_signature import ConfigSignature, get_config_signature
from agentkit.config.runtime_paths import existing_project_file, project_root

logger = logging.getLogger(__name__)

EXTENSIONS_CONFIG_FILENAME = "extensions_config.json"

#: 显式断言"必须用这个文件"的两条通道。前一条是本仓库前缀，后一条与设计文档一致。
EXTENSIONS_CONFIG_ENV_NAMES = (
    "MYFLOW_EXTENSIONS_CONFIG_PATH",
    "DEER_FLOW_EXTENSIONS_CONFIG_PATH",
)

#: 进程内写锁。**必须是 threading.Lock，不是 asyncio.Lock**（§2.2）：RMW 跑在 worker
#: 线程里，await to_thread 外层的 task 被取消不能把锁提前放掉。
extensions_config_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# §2.1 文件形状
# --------------------------------------------------------------------------


class McpUserAuthConfig(BaseModel):
    """per-user 凭据层（§5.5）。users 的值可以写 $ENV；GET 时整层打码。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    users: dict[str, dict[str, str]] = Field(default_factory=dict)
    on_missing: Literal["fail_closed", "passthrough"] = "fail_closed"


class McpOAuthConfig(BaseModel):
    """OAuth token 端点（§5.5 层 ①）。client_secret / refresh_token 是凭据：GET 时打码。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: Literal["client_credentials", "refresh_token"] = "client_credentials"
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    refresh_token: str | None = None
    header: str = "Authorization"
    expiry_margin_seconds: float = 30.0


class McpServerConfig(BaseModel):
    """一台 MCP 服务器。读路径宽松（extra=allow），写路径由 Gateway 的 McpServerWrite 收紧。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    enabled: bool = True
    type: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    tool_name_prefix: bool = True
    session_init_timeout: float | None = 60
    tool_call_timeout: float | None = 60
    headers_from_context: dict[str, str] = Field(default_factory=dict)
    user_auth: McpUserAuthConfig | None = None
    oauth: McpOAuthConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_transport_alias(cls, data: Any) -> Any:
        """transport 是 MCP 规范的别名；两个都写时 type 赢（§2.1）。别名不写回文件。"""
        if not isinstance(data, Mapping):
            return data
        raw = dict(data)
        transport = raw.pop("transport", None)
        if raw.get("type") is None and transport is not None:
            raw["type"] = transport
        return raw

    @property
    def transport_kind(self) -> str:
        """规范化 transport 名（小写、中划线换下划线），给 MCP 客户端选 transport 用。"""
        return str(self.type).strip().lower().replace("-", "_")

    @property
    def is_stdio(self) -> bool:
        return self.transport_kind == "stdio"

    def to_file_dict(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        for key in ("args", "env", "headers", "headers_from_context"):
            if not data.get(key):
                data.pop(key, None)
        for key in ("session_init_timeout", "tool_call_timeout"):
            value = data.get(key)
            if isinstance(value, float) and value.is_integer():
                data[key] = int(value)
        return data


class SkillToggleConfig(BaseModel):
    """JSON 里的技能启用状态。缺省 true：扫描到的新技能默认开（§3.2）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class ExtensionsConfig(BaseModel):
    """整份 extensions_config.json。mcpInterceptors 这类运维键原样保留在 model_extra。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
    skills: dict[str, SkillToggleConfig] = Field(default_factory=dict)
    #: 运维文件专属：它会 import 代码，所以 Gateway 不提供写它的 API（§2.4/§11）。
    mcp_interceptors: list[str] = Field(default_factory=list, alias="mcpInterceptors")

    @classmethod
    def resolve_config_path(cls, config_path: str | Path | None = None) -> Path | None:
        return resolve_extensions_config_path(config_path)

    @classmethod
    def from_file(cls, config_path: str | Path | None = None) -> Self:
        """显式/环境变量路径缺失 → FileNotFoundError；搜索模式找不到 → 默认空配置。"""
        path = cls.resolve_config_path(config_path)
        if path is None:
            return cls()
        return cls.model_validate(read_extensions_config_file(path))

    def to_file_dict(self) -> dict[str, Any]:
        """写回文件用的形状：by_alias（mcpServers），不包括内存字段名 mcp_servers。"""
        data: dict[str, Any] = {
            "mcpServers": {
                name: server.to_file_dict() for name, server in self.mcp_servers.items()
            }
        }
        if self.skills:
            data["skills"] = {
                name: {"enabled": skill.enabled} for name, skill in self.skills.items()
            }
        if self.mcp_interceptors:
            data["mcpInterceptors"] = list(self.mcp_interceptors)
        for key, value in (self.model_extra or {}).items():
            data.setdefault(key, value)
        return data


# --------------------------------------------------------------------------
# 路径解析（与 M0 同一套四级优先级）
# --------------------------------------------------------------------------


def _env_config_path() -> str | None:
    for name in EXTENSIONS_CONFIG_ENV_NAMES:
        value = os.getenv(name)
        if value:
            return value
    return None


def resolve_extensions_config_path(explicit: str | Path | None = None) -> Path | None:
    """显式参数 → 环境变量 → 项目根搜索。

    前两级是"运维明确断言必须用这个文件"，文件不存在 → FileNotFoundError；
    只有搜索模式找不到才返回 None，那是合法的"没配扩展"（§2.1）。
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"Extensions config file {path} does not exist")
        return path
    env_value = _env_config_path()
    if env_value:
        path = Path(env_value)
        if not path.is_file():
            raise FileNotFoundError(
                f"Extensions config file {env_value} (from environment) does not exist"
            )
        return path
    return existing_project_file((EXTENSIONS_CONFIG_FILENAME,))


def writable_extensions_config_path(explicit: str | Path | None = None) -> Path:
    """写路径：显式/环境变量缺失仍然大声失败；搜索模式没找到就用项目根下的默认文件。"""
    return _resolve_for_write(explicit)[0]


def _resolve_for_write(explicit: str | Path | None = None) -> tuple[Path, bool]:
    """(写路径, 允许文件还不存在)。只有"搜索模式没配"才允许凭空创建。"""
    found = resolve_extensions_config_path(explicit)
    if found is not None:
        return found, False
    return project_root() / EXTENSIONS_CONFIG_FILENAME, True


def read_extensions_config_file(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    """读原始 JSON。统一 UTF-8（Windows 默认 GBK 会把中文技能名读乱）。"""
    return read_json_file(path, missing_ok=missing_ok)


# --------------------------------------------------------------------------
# §2.2 双锁 + 原子写（实现在 file_lock.py / atomic_json.py，skills_prefs.json 复用同一套）
# --------------------------------------------------------------------------


def extensions_config_file_lock(
    path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> AbstractContextManager[None]:
    """跨进程文件咨询锁。锁文件是同目录下的 .<name>.lock，建好就不删。"""
    return advisory_file_lock(path, timeout=timeout)


def atomic_write_extensions_config(path: Path, data: Mapping[str, Any]) -> None:
    """同目录临时文件 + fsync + os.replace()，只有 EBUSY 才退化（§2.2）。"""
    atomic_write_json(path, data)


# §2.2 读-改-写：锁覆盖整个循环
# --------------------------------------------------------------------------


def update_extensions_config(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """完整 RMW：读原始 JSON → merge → atomic_write → reload，全程持双锁（§2.2）。

    读的是原始 JSON 而不是 from_file 的结果：运维写在文件里的 mcpInterceptors 之类的键
    要原样保留。reload_extensions_config() 仍然走 from_file，所以坏配置在写完就报错。

    mutate 拿到磁盘上的原始 JSON；它抛异常就不写文件。锁在**执行 RMW 的这个线程里**
    获取，调用方必须把整段放进 asyncio.to_thread —— 包在 await 外面时 task 一取消锁
    就放，后台线程还在写，第二个 writer 会闯进去。
    """
    path, may_create = _resolve_for_write()
    with extensions_config_write_lock:
        with extensions_config_file_lock(path):
            # 显式/环境变量断言的路径中途消失要大声失败，不能悄悄写出一份新的空配置。
            data = read_extensions_config_file(path, missing_ok=may_create)
            mutate(data)
            atomic_write_extensions_config(path, data)
            reload_extensions_config()
    return data


# --------------------------------------------------------------------------
# §2.3 缓存：解析后的路径 + 内容签名
# --------------------------------------------------------------------------

_cache_guard = threading.Lock()
_cached_config: ExtensionsConfig | None = None
_cached_path: Path | None = None
_cached_signature: ConfigSignature | None = None
_cache_loaded = False


def get_extensions_config() -> ExtensionsConfig:
    """按"解析后的路径 + 内容签名"缓存。

    签名是 (mtime, size, sha256) 三元组，不是 mtime 大小比较：同一秒内两次编辑、
    git checkout 让 mtime 倒退、cp -p / tar 恢复、换到另一个更老的文件，都要判成已变更。
    """
    path = resolve_extensions_config_path()
    signature = get_config_signature(path) if path is not None else None
    with _cache_guard:
        if _cache_loaded and _cached_path == path and _cached_signature == signature:
            assert _cached_config is not None
            return _cached_config
    return _load_and_store(path, signature)


def reload_extensions_config() -> ExtensionsConfig:
    """写完配置后必须调用（§2.2 第 4 步）：丢掉缓存重新解析，仍在写锁范围之内。"""
    path = resolve_extensions_config_path()
    signature = get_config_signature(path) if path is not None else None
    return _load_and_store(path, signature)


def reset_extensions_config_cache() -> None:
    """只清本进程的缓存。别的 worker 靠签名自己失效。"""
    global _cached_config, _cached_path, _cached_signature, _cache_loaded
    with _cache_guard:
        _cached_config = None
        _cached_path = None
        _cached_signature = None
        _cache_loaded = False


def _load_and_store(path: Path | None, signature: ConfigSignature | None) -> ExtensionsConfig:
    """存的是读之前那一刻的签名：读的过程中文件被改，下次 get 会看到签名对不上而重载。"""
    global _cached_config, _cached_path, _cached_signature, _cache_loaded
    config = ExtensionsConfig.from_file(path) if path is not None else ExtensionsConfig()
    with _cache_guard:
        _cached_config = config
        _cached_path = path
        _cached_signature = signature
        _cache_loaded = True
    return config