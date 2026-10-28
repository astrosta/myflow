"""内置技能加载器（M7 §3.1）。

内置技能随项目发布，只读、不走 Git、不进数据库。它们和 public/custom 共用
`scan_skill_category` 的扫目录规则，这里只负责三件事：

* 按配置开关和根目录是否存在决定要不要扫；
* 用目录指纹缓存扫描结果，SKILL.md 一改就重新扫；
* 根目录缺失时只告警一次，避免每次列技能都刷日志；目录出现后必须能恢复。

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
_warned_missing_roots: set[Path] = set()
_lock = threading.Lock()

__all__ = ["builtin_names", "clear_builtin_cache", "load_builtin_skills"]


def load_builtin_skills(config: SkillsConfig) -> tuple[Skill, ...]:
    """返回内置技能；关闭时返回空元组，根目录缺失时告警一次并返回空元组。"""
    if not config.builtin.enabled:
        return ()

    root = config.builtin.root_dir().resolve()
    with _lock:
        if not root.is_dir():
            # 不缓存空结果：目录晚一点出现时，下一次调用必须能发现它。
            _cache.pop(root, None)
            if root not in _warned_missing_roots:
                logger.warning("built-in skills directory is missing: %s", root)
                _warned_missing_roots.add(root)
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
        _cache[root] = (fingerprint, skills)
        return skills


def clear_builtin_cache() -> None:
    """丢掉扫描结果和"已告警"标记；配置重载或手动 reload 时调用。"""
    with _lock:
        _cache.clear()
        _warned_missing_roots.clear()


def builtin_names(config: SkillsConfig) -> frozenset[str]:
    """内置技能名集合，给保留名检查与覆盖判断用。"""
    return frozenset(skill.name for skill in load_builtin_skills(config))


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
