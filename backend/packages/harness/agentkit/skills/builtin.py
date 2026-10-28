"""内置技能加载器（M7 §3.1）。

内置技能随项目发布，只读、不走 Git、不进数据库。它们和 public/custom 共用
`scan_skill_category` 的扫目录规则，这里只负责三件事：

* 按配置开关和根目录是否可读决定要不要扫；
* 用目录指纹缓存扫描结果，SKILL.md 一改就重新扫；
* 根目录缺失或读不了时只告警一次，避免每次列技能都刷日志；目录恢复后必须能重新发现技能。

根目录缺失或读不了（PermissionError、is_dir() 之后被并发删除、resolve() 失败等）
一律退化成空元组，绝不把异常抛进启动、列表或提示词构建——内置技能是可选资源，
它坏了不能拖垮整个进程。失败结果不进缓存。

缓存键是解析后的根目录，值是 `(fingerprint, tuple[Skill, ...])`。指纹只包含
一级子目录及其 SKILL.md 的 `st_mtime_ns` / `st_size`：新增、删除、改名或改写
技能都会让指纹变化，而无关文件不会触发重扫。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from agentkit.config.skills_config import SkillsConfig
from agentkit.skills.domain import SkillScope
from agentkit.skills.scan import scan_skill_category
from agentkit.skills.types import SKILL_FILE_NAME, Skill, SkillCategory

logger = logging.getLogger(__name__)

_Fingerprint = tuple[tuple[str, int, int], ...]
_CacheEntry = tuple[_Fingerprint, tuple[Skill, ...]]

_cache: dict[Path, _CacheEntry] = {}
#: 已经告警过的根目录（缺失或读不了），每个根只报一次。
_warned_roots: set[Path] = set()
_lock = threading.Lock()

__all__ = ["builtin_names", "clear_builtin_cache", "load_builtin_skills"]


def load_builtin_skills(config: SkillsConfig) -> tuple[Skill, ...]:
    """返回内置技能；关闭、根目录缺失或读不了时返回空元组（告警一次，失败不缓存）。"""
    if not config.builtin.enabled:
        return ()

    configured_root = config.builtin.root_dir()
    with _lock:
        try:
            root = configured_root.resolve()
        except OSError as exc:
            # 连路径都解析不了（父目录权限、磁盘异常）：用配置里的路径当告警 key。
            _warn_unusable(configured_root, exc)
            return ()

        try:
            if not root.is_dir():
                # 不缓存空结果：目录晚一点出现时，下一次调用必须能发现它。
                _cache.pop(root, None)
                _warn_unusable(root)
                return ()

            fingerprint = _fingerprint(root)
            cached = _cache.get(root)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]

            skills = tuple(
                scan_skill_category(
                    root,
                    category=SkillCategory.BUILTIN,
                    container_path=config.container_path,
                    scope=SkillScope.BUILTIN,
                    enabled=True,
                    allow_symlink_dirs=False,
                )
            )
        except OSError as exc:
            # is_dir() 通过不等于读得到：权限不足、检查后被并发删除、IO 错误都在这里
            # 退化成空元组。失败结果不缓存，目录/权限恢复后下一次调用就能重新发现。
            _cache.pop(root, None)
            _warn_unusable(root, exc)
            return ()

        _cache[root] = (fingerprint, skills)
        return skills


def clear_builtin_cache() -> None:
    """丢掉扫描结果和"已告警"标记；配置重载或手动 reload 时调用。"""
    with _lock:
        _cache.clear()
        _warned_roots.clear()


def builtin_names(config: SkillsConfig) -> frozenset[str]:
    """内置技能名集合，给保留名检查与覆盖判断用。"""
    return frozenset(skill.name for skill in load_builtin_skills(config))


def _warn_unusable(root: Path, error: OSError | None = None) -> None:
    """根目录缺失或读不了：每个根只告警一次。调用方已持有 _lock。"""
    if root in _warned_roots:
        return
    _warned_roots.add(root)
    if error is None:
        logger.warning("built-in skills directory is missing: %s", root)
    else:
        logger.warning("built-in skills directory is unreadable: %s (%s)", root, error)


def _fingerprint(root: Path) -> _Fingerprint:
    """一级子目录里 SKILL.md 的 (目录名, mtime_ns, size)；没有 SKILL.md 的目录无关。"""
    entries: list[tuple[str, int, int]] = []
    for entry in root.iterdir():
        try:
            stat = (entry / SKILL_FILE_NAME).stat()
        except OSError:
            continue
        entries.append((entry.name, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))
