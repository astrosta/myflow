"""M9 §2.8：存储分桶迁移。

迁移不是"挪文件"：它同时改的是**谁能看见什么**。四条规则里能离线验证的三条
（可重入、校验再删、旧 agents 只报告）都在这里钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.scripts import migrate_user_buckets as migration

ADMIN = "admin-uuid"


def _legacy_thread(base: Path, thread_id: str, *, content: str = "hello") -> Path:
    workspace = base / "threads" / thread_id / "user-data" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "a.py").write_text(content, encoding="utf-8")
    return base / "threads" / thread_id


def test_dry_run_prints_the_plan_and_moves_nothing(tmp_path: Path, capsys) -> None:
    legacy = _legacy_thread(tmp_path, "t1")

    failures = migration.migrate(base=tmp_path, dry_run=True, admin_id=ADMIN)

    assert failures == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "1 thread(s) would move into users/" + ADMIN in out
    assert legacy.exists()
    assert not (tmp_path / "users").exists()


def test_apply_moves_every_legacy_thread_into_the_admin_bucket(tmp_path: Path) -> None:
    _legacy_thread(tmp_path, "t1")
    _legacy_thread(tmp_path, "t2")

    failures = migration.migrate(base=tmp_path, dry_run=False, admin_id=ADMIN)

    assert failures == 0
    moved = tmp_path / "users" / ADMIN / "threads"
    assert sorted(path.name for path in moved.iterdir()) == ["t1", "t2"]
    assert (moved / "t1" / "user-data" / "workspace" / "a.py").read_text(encoding="utf-8") == "hello"
    assert list((tmp_path / "threads").iterdir()) == []


def test_an_existing_target_is_skipped_and_never_overwritten(tmp_path: Path) -> None:
    """覆盖 = 把新桶里的数据换成旧的。宁可跳过，让运维自己看。"""
    _legacy_thread(tmp_path, "t1", content="OLD")
    target = tmp_path / "users" / ADMIN / "threads" / "t1" / "user-data" / "workspace"
    target.mkdir(parents=True)
    (target / "a.py").write_text("NEW", encoding="utf-8")

    failures = migration.migrate(base=tmp_path, dry_run=False, admin_id=ADMIN)

    assert failures == 0
    assert (target / "a.py").read_text(encoding="utf-8") == "NEW"
    assert (tmp_path / "threads" / "t1" / "user-data" / "workspace" / "a.py").exists(), "源也不能被删"


def test_a_second_run_is_idempotent(tmp_path: Path) -> None:
    _legacy_thread(tmp_path, "t1")
    assert migration.migrate(base=tmp_path, dry_run=False, admin_id=ADMIN) == 0

    assert migration.migrate(base=tmp_path, dry_run=False, admin_id=ADMIN) == 0

    assert (tmp_path / "users" / ADMIN / "threads" / "t1" / "user-data" / "workspace" / "a.py").exists()


def test_one_failure_does_not_stop_the_rest(tmp_path: Path, monkeypatch, capsys) -> None:
    _legacy_thread(tmp_path, "bad")
    _legacy_thread(tmp_path, "good")
    real_move = migration._move_verified

    def _flaky(src: Path, dst: Path) -> None:
        if src.name == "bad":
            raise OSError("disk went away")
        real_move(src, dst)

    monkeypatch.setattr(migration, "_move_verified", _flaky)

    failures = migration.migrate(base=tmp_path, dry_run=False, admin_id=ADMIN)

    assert failures == 1
    assert (tmp_path / "users" / ADMIN / "threads" / "good").exists()
    assert (tmp_path / "threads" / "bad").exists()


def test_a_failed_cross_device_copy_keeps_the_source(tmp_path: Path, monkeypatch) -> None:
    """跨文件系统时 move 退化成"拷贝 + 删"。校验不通过就**不能**删源。"""
    src = tmp_path / "threads" / "t1"
    (src / "user-data").mkdir(parents=True)
    (src / "user-data" / "a.py").write_text("hello", encoding="utf-8")
    dst = tmp_path / "users" / ADMIN / "threads" / "t1"
    dst.parent.mkdir(parents=True)

    monkeypatch.setattr(migration, "_same_device", lambda a, b: False)
    monkeypatch.setattr(
        migration,
        "_tree_signature",
        lambda root: (0, 0) if "users" in root.parts else (1, 5),
    )

    with pytest.raises(OSError, match="verification failed"):
        migration._move_verified(src, dst)

    assert src.exists() and (src / "user-data" / "a.py").read_text(encoding="utf-8") == "hello"
    assert not dst.exists(), "半个目录比没有更糟：校验失败要回滚目标"


def test_the_tree_signature_counts_entries_and_bytes(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("12345", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("67", encoding="utf-8")

    assert migration._tree_signature(root) == (4, 7)  # root + sub + 2 文件


def test_the_legacy_agents_dir_is_only_reported(tmp_path: Path, capsys) -> None:
    """规则 4：旧共享布局迁移后仍可读，但**任何写/删之前必须先迁**。"""
    (tmp_path / "agents" / "helper").mkdir(parents=True)

    migration._report_legacy_agents(tmp_path)

    out = capsys.readouterr().out
    assert "agents" in out
    assert "before any write/delete" in out
    assert (tmp_path / "agents" / "helper").exists()
