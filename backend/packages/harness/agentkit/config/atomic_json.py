"""原子写 JSON：同目录临时文件 + fsync + os.replace()（M7 §2.2）。

直接覆写原文件意味着写到一半崩溃会留下截断的 JSON——下次启动整份配置解析失败。
Docker 把单个文件挂载进容器时那个文件是独立 mount point，rename() 会返回 EBUSY，
只有那一种 errno 退化为就地覆写（非原子），其它 errno 一律抛。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import threading
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from agentkit.config.file_lock import fsync_directory_best_effort

logger = logging.getLogger(__name__)

_ebusy_warned: set[str] = set()
_ebusy_warned_guard = threading.Lock()


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """同目录临时文件 + fsync + os.replace()，只有 EBUSY 才退化。"""
    target = path.resolve(strict=False) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = _existing_file_mode(target)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        # ensure_ascii=False：技能名、description 经常是中文，写成 \uXXXX 人肉排查看不见。
        json.dump(dict(data), tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_path = tmp.name
    try:
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, target)  # 跟随符号链接目标
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            _unlink_quietly(temp_path)
            raise  # 其它 errno 绝不能退化
        _overwrite_in_place(target, temp_path)
    fsync_directory_best_effort(target.parent)


def read_json_file(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    """读原始 JSON。统一 UTF-8（Windows 默认 GBK 会把中文技能名读乱）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if not text.strip():
        # 刚 cp 出来的空文件当"没配"；截断的 JSON 仍然会大声报错。
        logger.warning("%s is empty; treating it as unconfigured", path)
        return {}
    data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object at the top level")
    return data


def _existing_file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _overwrite_in_place(target: Path, temp_path: str) -> None:
    """Docker 单文件挂载：那条退化路径**不是原子的**，写到一半崩溃就是截断的 JSON。"""
    _warn_ebusy_once(target)
    with open(temp_path, encoding="utf-8") as source:
        with open(target, "w", encoding="utf-8", newline="") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
    _unlink_quietly(temp_path)


def _warn_ebusy_once(target: Path) -> None:
    key = str(target)
    with _ebusy_warned_guard:
        first = key not in _ebusy_warned
        _ebusy_warned.add(key)
    if first:
        logger.warning(
            "rename() returned EBUSY for %s (single-file mount?); "
            "falling back to a non-atomic in-place overwrite: a crash mid-write truncates it",
            target,
        )
    else:
        logger.debug("EBUSY fallback for %s", target)


def _unlink_quietly(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.debug("cannot remove temp file %s", path, exc_info=True)
