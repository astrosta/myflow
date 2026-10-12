"""M7 §2.3：缓存失效比内容签名，不比 mtime 大小。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentkit.config.extensions_config import (
    EXTENSIONS_CONFIG_ENV_NAMES,
    ExtensionsConfig,
    get_extensions_config,
    reset_extensions_config_cache,
    resolve_extensions_config_path,
)
from agentkit.config.file_signature import get_config_signature
from agentkit.mcp.cache import (
    extensions_config_changed,
    extensions_config_snapshot,
    invalidate_mcp_cache,
    mcp_cache_generation,
    register_session_retirer,
    reset_local_mcp_cache,
    wait_for_generation_change,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for name in EXTENSIONS_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    reset_extensions_config_cache()
    yield
    reset_extensions_config_cache()


def _write(path: Path, text: str, *, mtime: float | None = None) -> None:
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_two_edits_in_the_same_second_are_a_change(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    _write(path, '{"skills": {"a": {}}}')
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    before = extensions_config_snapshot()
    same_size_edit = '{"skills": {"b": {}}}'

    _write(path, same_size_edit, mtime=path.stat().st_mtime)

    after = get_config_signature(path)
    assert after[1] == before.signature[1]  # 大小一样
    assert after[2] != before.signature[2]  # 只有内容哈希能看出来
    assert extensions_config_changed(before) is True


def test_an_older_mtime_is_still_a_change(tmp_path, monkeypatch):
    """git checkout / cp -p / tar 恢复会让 mtime 倒退，mtime > 比较会漏掉。"""
    path = tmp_path / "extensions_config.json"
    _write(path, '{"skills": {"a": {}}}')
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    before = extensions_config_snapshot()

    _write(path, '{"skills": {"a": {}, "b": {}}}', mtime=before.signature[0] - 60)

    assert path.stat().st_mtime < before.signature[0]
    assert extensions_config_changed(before) is True


def test_switching_to_an_older_file_is_a_change(tmp_path, monkeypatch):
    newer = tmp_path / "new.json"
    older = tmp_path / "old.json"
    _write(newer, '{"skills": {"a": {}}}')
    _write(older, '{"skills": {"b": {}}}', mtime=newer.stat().st_mtime - 3600)
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(newer))
    before = extensions_config_snapshot()

    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(older))

    assert extensions_config_changed(before) is True


def test_an_untouched_file_is_not_a_change(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    _write(path, '{"skills": {"a": {"enabled": false}}}')
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    before = extensions_config_snapshot()

    assert extensions_config_changed(before) is False
    assert get_extensions_config() is get_extensions_config()  # 命中缓存
    assert get_extensions_config().skills["a"].enabled is False


def test_editing_the_file_reloads_the_cache(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    _write(path, '{"skills": {"a": {"enabled": true}}}')
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    assert get_extensions_config().skills["a"].enabled is True
    before = extensions_config_snapshot()

    _write(path, '{"skills": {"a": {"enabled": false}}}', mtime=path.stat().st_mtime)

    assert extensions_config_changed(before) is True
    assert get_extensions_config().skills["a"].enabled is False


def test_a_vanished_explicit_path_is_unconfigured_on_the_hot_path(tmp_path, monkeypatch):
    """热路径当"没配"，真正的 from_file() 调用者仍然要大声失败。"""
    missing = tmp_path / "gone.json"
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(missing))

    snapshot = extensions_config_snapshot()

    assert snapshot.path is None
    assert snapshot.signature is None
    with pytest.raises(FileNotFoundError):
        ExtensionsConfig.from_file()
    with pytest.raises(FileNotFoundError):
        resolve_extensions_config_path()


def test_invalidation_bumps_the_generation_and_wakes_waiters():
    generation = mcp_cache_generation()

    new_generation = reset_local_mcp_cache()

    assert new_generation == generation + 1
    assert mcp_cache_generation() == new_generation
    assert wait_for_generation_change(generation, timeout=1.0) == new_generation
    assert wait_for_generation_change(new_generation, timeout=0.01) == new_generation


def test_invalidation_retires_sessions(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    _write(path, "{}")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    retired: list[str] = []
    unregister = register_session_retirer(retired.append)
    try:
        invalidate_mcp_cache(reason="api")
    finally:
        unregister()

    assert retired == ["api"]
    invalidate_mcp_cache(reason="api")
    assert retired == ["api"]  # 反注册之后不再回调


def test_a_change_during_load_takes_the_retire_path(tmp_path, monkeypatch):
    """加载中签名变了 → 丢弃结果时必须走和正常失效一样的会话退役路径（§2.3）。"""
    path = tmp_path / "extensions_config.json"
    _write(path, '{"skills": {"a": {"enabled": true}}}')
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    retired: list[str] = []
    unregister = register_session_retirer(retired.append)
    try:
        before = extensions_config_snapshot()
        _write(path, '{"skills": {"a": {"enabled": false}}}', mtime=path.stat().st_mtime)

        changed = extensions_config_changed(before)
        if changed:
            invalidate_mcp_cache(reason="config_changed_during_load")
    finally:
        unregister()

    assert changed is True
    assert retired == ["config_changed_during_load"]


def test_write_then_read_sees_the_new_content(tmp_path, monkeypatch):
    from agentkit.config.extensions_config import update_extensions_config

    path = tmp_path / "extensions_config.json"
    _write(path, json.dumps({"skills": {"a": {"enabled": True}}}))
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    assert get_extensions_config().skills["a"].enabled is True

    update_extensions_config(lambda data: data["skills"]["a"].update({"enabled": False}))

    assert get_extensions_config().skills["a"].enabled is False
