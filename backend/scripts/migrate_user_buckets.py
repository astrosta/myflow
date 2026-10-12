"""`uv run python -m backend.scripts.migrate_user_buckets`（M9 §2.8）。

宿主机布局从 M2 的扁平目录升为：

```
{base}/users/{storage_user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}
```

**迁移不是"挪文件"，它同时改的是"谁能看见什么"**（§2.8 的四条业务规则）：

1. **先建 `users` 行，再迁桶。** 桶名派生自 `users.id`，没有账号行就没有目标路径。
   所以运行前必须已经有一个 admin（`/setup` 或 `reset_admin`），脚本不替你猜。
2. **旧布局 `{base}/threads/{tid}/...` 全部归 admin 桶。** 认证之前的数据没有属主，
   唯一诚实的处置是"归第一个 admin"，日志里记下"N 条 thread 归入谁"，让运维能事后手工改。
3. **迁移期间只读。** 脚本第一步检查有没有活跃 run，有就**拒绝启动**迁移。
4. **旧路径只读兼容一个版本。** `{base}/agents/` 只报告不搬（写/删之前必须先迁）。

每一步都可重入：目标已存在 → **跳过**（幂等），**绝不覆盖**——覆盖是把新桶里的数据换成旧的。
跨文件系统时 `move` 退化成"拷贝 + 删"，所以先校验条目数与总字节数，再删源。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from agentkit.config import get_app_config
from agentkit.config.paths import get_paths
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from agentkit.utils import make_safe_user_id
from backend.app.gateway.auth.store import make_user_store

logger = logging.getLogger(__name__)

LEGACY_THREADS_DIRNAME = "threads"
LEGACY_AGENTS_DIRNAME = "agents"
USERS_DIRNAME = "users"


def legacy_thread_dirs(base: Path) -> list[Path]:
    legacy = base / LEGACY_THREADS_DIRNAME
    if not legacy.is_dir():
        return []
    return sorted(path for path in legacy.iterdir() if path.is_dir())


def target_thread_dir(base: Path, *, storage_user_id: str, thread_id: str) -> Path:
    return base / USERS_DIRNAME / storage_user_id / LEGACY_THREADS_DIRNAME / thread_id


def migrate(*, base: Path, dry_run: bool, admin_id: str) -> int:
    """返回失败数。单个用户失败继续，最后非零退出（§2.8）。"""
    storage_user_id = make_safe_user_id(admin_id)
    failures = 0
    moved = 0
    for legacy_dir in legacy_thread_dirs(base):
        target = target_thread_dir(base, storage_user_id=storage_user_id, thread_id=legacy_dir.name)
        if target.exists():
            # 幂等：目标已存在就跳过。**不要覆盖**——那是把新桶的数据换成旧的。
            logger.warning("skip %s: %s already exists", legacy_dir, target)
            continue
        if dry_run:
            print(f"[dry-run] {legacy_dir} -> {target}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _move_verified(legacy_dir, target)
        except OSError:
            logger.exception("Failed to migrate %s", legacy_dir)
            failures += 1
            continue
        moved += 1
        print(f"migrated {legacy_dir} -> {target}")

    if dry_run:
        print(
            f"[dry-run] {len(legacy_thread_dirs(base))} thread(s) would move into "
            f"users/{storage_user_id} (admin {admin_id})"
        )
    else:
        print(f"{moved} thread(s) moved into users/{storage_user_id} (admin {admin_id})")
    return failures


def _tree_signature(root: Path) -> tuple[int, int]:
    """(条目数, 总字节数)。跨文件系统拷贝之后的校验依据（§2.8）。"""
    entries = 0
    total = 0
    for path in _walk(root):
        entries += 1
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:  # pragma: no cover —— 校验时的竞态：宁可报错也不要静默删源
            raise
    return entries, total


def _walk(root: Path) -> Iterator[Path]:
    yield root
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            yield from _walk(child)
        else:
            yield child


def _same_device(src: Path, dst: Path) -> bool:
    return src.stat().st_dev == dst.parent.stat().st_dev


def _move_verified(src: Path, dst: Path) -> None:
    """同盘 rename；跨盘拷贝 + 校验 + 删源。中途崩溃不会留下"半个目录被当成已完成"。"""
    if _same_device(src, dst):
        shutil.move(str(src), str(dst))
        return
    shutil.copytree(src, dst, symlinks=True)
    if _tree_signature(src) != _tree_signature(dst):
        shutil.rmtree(dst, ignore_errors=True)
        raise OSError(f"verification failed for {src}: refusing to delete the source")
    shutil.rmtree(src)


async def _has_active_runs() -> bool:
    """规则 3：脚本跑的时候 Gateway 必须停（或至少不接写入）。

    有活跃 run 就拒绝——否则新写入会落到旧路径、被下一次迁移漏掉。
    """
    from agentkit.persistence.run import make_run_store

    run_store = make_run_store(get_session_factory())
    return bool(await run_store.list_inflight())


async def _resolve_admin_id(*, email: str | None) -> str | None:
    store = make_user_store(get_session_factory())
    if email:
        user = await store.get_by_email(email)
        return user["id"] if user else None
    return await store.first_admin_id()


async def run(*, base: Path, dry_run: bool, email: str | None) -> int:
    app_config = get_app_config()
    await init_engine_from_config(app_config.database)
    try:
        if await _has_active_runs():
            print(
                "refusing to migrate: an active run exists. Stop the gateway (or at least its "
                "writers) first — otherwise new writes land in the old layout (M9 §2.8 rule 3).",
                file=sys.stderr,
            )
            return 2
        admin_id = await _resolve_admin_id(email=email)
        if admin_id is None:
            print(
                "no admin account found: create one first (POST /api/v1/auth/initialize or "
                "python -m backend.app.gateway.auth.reset_admin). Buckets are named after users.id, "
                "so there is no target path without an account (M9 §2.8 rule 1).",
                file=sys.stderr,
            )
            return 2
        failures = migrate(base=base, dry_run=dry_run, admin_id=admin_id)
        _report_legacy_agents(base)
        return 1 if failures else 0
    finally:
        await close_engine()


def _report_legacy_agents(base: Path) -> None:
    """规则 4：`{base}/agents/` 迁移后仍可读，但**任何写/删之前必须先迁**。"""
    legacy = base / LEGACY_AGENTS_DIRNAME
    if legacy.is_dir():
        print(
            f"note: {legacy} is still the M2 shared layout. It stays readable, but migrate it "
            "before any write/delete (M9 §2.8 rule 4)."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move flat thread buckets into per-user buckets (M9 §2.8).")
    parser.add_argument("--base", default=None, help="host base dir; default: the sandbox base dir")
    parser.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    parser.add_argument("--email", default=None, help="admin to own the legacy data; default: the first admin")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    base = Path(args.base).resolve() if args.base else get_paths().base_dir
    dry_run = not args.apply or args.dry_run
    return asyncio.run(run(base=base, dry_run=dry_run, email=args.email))


if __name__ == "__main__":
    raise SystemExit(main())
