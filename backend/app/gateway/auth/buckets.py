"""用户存储桶的删除标记（M9 §2.13 不变量 4）。

删号是**软删 + 回收期**：行保留（`deleted_at` 非空）、令牌全失效、桶**先标记后删**。
标记文件让保留策略任务（与 M4 §10 同一套扫描）知道"这个桶的宽限期从哪天开始"，
也让 admin 的"恢复"有依据——恢复 = 删掉标记 + 清 `deleted_at`。

为什么单独放文件而不是只信数据库：桶里可能有运维手工放的东西，
而"数据还看得见吗"这个问题必须在**路径**这一层也能回答（§2.13 开头那句话）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agentkit.config.paths import get_paths
from agentkit.utils import make_safe_user_id

logger = logging.getLogger(__name__)

MARKER_NAME = "deletion.json"


def user_bucket_dir(storage_user_id: str) -> Path:
    return get_paths().base_dir / "users" / make_safe_user_id(storage_user_id)


def marker_path(storage_user_id: str) -> Path:
    return user_bucket_dir(storage_user_id) / MARKER_NAME


def mark_user_bucket_deleted(storage_user_id: str, *, deleted_at: str) -> Path | None:
    """写标记。桶还不存在（用户从没跑过 run）时返回 None，不为了标记而建目录。"""
    bucket = user_bucket_dir(storage_user_id)
    if not bucket.exists():
        return None
    target = marker_path(storage_user_id)
    target.write_text(json.dumps({"deleted_at": deleted_at}, ensure_ascii=False), encoding="utf-8")
    return target


def unmark_user_bucket(storage_user_id: str) -> bool:
    target = marker_path(storage_user_id)
    if target.exists():
        target.unlink()
        return True
    return False


def is_user_bucket_marked_deleted(storage_user_id: str) -> bool:
    return marker_path(storage_user_id).exists()
