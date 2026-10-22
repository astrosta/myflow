"""skills_prefs.json：技能**个人**偏好的存储路径与锁（M7 §2.4）。

两层开关存在两份文件、两把锁里（互不阻塞）：

    effective_enabled(user, skill) = global_enabled(skill) and user_pref(user, skill)

- 全局开关在 extensions_config.json 的 skills.<name>.enabled，admin 改，动所有人。
- 个人偏好在 skills_prefs.json 的 {user_id: {skill_name: bool}}，任何登录用户改，只动自己。
- 用户不能把一个被全局禁用的技能打开；管理员也不该替用户决定"你用不用"。

M7 的存储是 JSON（M9 有 users 表之后可以迁到 user_skill_prefs，接口一行不改）。
无认证时（M7 单用户）user_id=None 落在 "__default__" 桶。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from agentkit.config.atomic_json import atomic_write_json, read_json_file
from agentkit.config.file_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, advisory_file_lock
from agentkit.config.runtime_paths import existing_project_file, project_root

logger = logging.getLogger(__name__)

SKILLS_PREFS_FILENAME = "skills_prefs.json"

#: 显式断言"必须用这个文件"的那条通道；与 extensions_config 同一套语义。
SKILLS_PREFS_ENV_NAMES = ("MYFLOW_SKILLS_PREFS_PATH",)

#: 无认证时的桶名。M9 有 users 表之后这个桶就是"匿名/单用户"。
DEFAULT_USER_BUCKET = "__default__"

#: 进程内写锁。和 extensions_config_write_lock **不是**同一把：两份文件互不阻塞。
skills_prefs_write_lock = threading.Lock()


def user_bucket(user_id: str | None) -> str:
    return user_id if user_id else DEFAULT_USER_BUCKET


def _env_prefs_path() -> str | None:
    for name in SKILLS_PREFS_ENV_NAMES:
        value = os.getenv(name)
        if value:
            return value
    return None


def resolve_skills_prefs_path(explicit: str | Path | None = None) -> Path | None:
    """显式参数 → 环境变量 → 项目根搜索。

    和 extensions_config.json 的语义**故意不同**：这份文件是 API 自己创建的运行时状态，
    还没写过的文件是"没人表过态"（合法默认），不是运维断言缺失，所以这里不抛。
    """
    if explicit is not None:
        return Path(explicit)
    env_value = _env_prefs_path()
    if env_value:
        return Path(env_value)
    return existing_project_file((SKILLS_PREFS_FILENAME,))


def writable_skills_prefs_path(explicit: str | Path | None = None) -> Path:
    """写路径：搜索模式没配就用项目根下的默认文件（个人开关第一次点就该能落盘）。"""
    found = resolve_skills_prefs_path(explicit)
    return found if found is not None else project_root() / SKILLS_PREFS_FILENAME


def skills_prefs_file_lock(
    path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> AbstractContextManager[None]:
    return advisory_file_lock(path, timeout=timeout)


def read_skills_prefs(path: Path | None = None) -> dict[str, dict[str, bool]]:
    """整份偏好。文件不存在当空。"""
    resolved = path if path is not None else resolve_skills_prefs_path()
    if resolved is None:
        return {}
    raw = read_json_file(resolved, missing_ok=True)
    cleaned: dict[str, dict[str, bool]] = {}
    for user, entries in raw.items():
        if not isinstance(entries, Mapping):
            logger.warning("Ignoring malformed skills preferences bucket %r", user)
            continue
        cleaned[str(user)] = {
            str(name): bool(enabled)
            for name, enabled in entries.items()
            if isinstance(enabled, bool)
        }
    return cleaned


def read_user_skill_prefs(user_id: str | None, *, path: Path | None = None) -> dict[str, bool]:
    """这个用户的个人开关；缺省 true（没表过态就是开）。"""
    return dict(read_skills_prefs(path).get(user_bucket(user_id), {}))


def update_skills_prefs(
    mutate: Callable[[dict[str, Any]], None], *, path: Path | None = None
) -> dict[str, Any]:
    """完整 RMW：双锁覆盖 读 → merge → 原子写。锁在调用它的那个线程里拿。

    **不 reload 全局配置**：extensions_config.json 没变（§2.4 步骤 4）。
    """
    target = path if path is not None else writable_skills_prefs_path()
    with skills_prefs_write_lock:
        with skills_prefs_file_lock(target):
            data = read_json_file(target, missing_ok=True)
            mutate(data)
            atomic_write_json(target, data)
    return data


def write_user_skill_pref(
    user_id: str | None, name: str, enabled: bool, *, path: Path | None = None
) -> dict[str, Any]:
    """只写 skills_prefs.json。[user_id][name] = enabled。"""
    bucket = user_bucket(user_id)

    def mutate(data: dict[str, Any]) -> None:
        entries = data.get(bucket)
        if not isinstance(entries, dict):
            entries = {}
        entries[name] = bool(enabled)
        data[bucket] = entries

    return update_skills_prefs(mutate, path=path)
