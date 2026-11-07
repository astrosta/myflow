"""M7 §2.2：并发读-改-写不丢更新、只有 EBUSY 退化、失败不留半截文件。

§9 说这一个文件和"凭据不回显"最值钱：配置丢状态的窗口只在并发写和崩溃里出现。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from agentkit.config.extensions_config import (
    EXTENSIONS_CONFIG_ENV_NAMES,
    atomic_write_extensions_config,
    extensions_config_file_lock,
    update_extensions_config,
)

_CHILD_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from agentkit.config.extensions_config import extensions_config_file_lock

    with extensions_config_file_lock(Path(sys.argv[1])):
        print("locked", flush=True)
        sys.stdin.readline()
    """
)


@pytest.fixture(autouse=True)
def _no_env_path(monkeypatch):
    for name in EXTENSIONS_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _seed(tmp_path: Path, monkeypatch, data: dict | None = None) -> Path:
    path = tmp_path / "extensions_config.json"
    path.write_text(
        json.dumps(data if data is not None else {"skills": {"a": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    return path


def _temp_files(directory: Path) -> list[str]:
    return sorted(item.name for item in directory.glob(".*.tmp"))


def _raise_errno(code: int):
    def _boom(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    return _boom


def test_concurrent_rmw_does_not_lose_updates(tmp_path, monkeypatch):
    """两个更新者打同一个文件：一把锁，两边都不能丢。"""
    path = _seed(tmp_path, monkeypatch, {"skills": {}})
    rounds = 12

    def churn_skills() -> None:
        for index in range(rounds):
            name = f"skill-{index}"

            def mutate(data: dict, name: str = name) -> None:
                data.setdefault("skills", {})[name] = {"enabled": True}

            update_extensions_config(mutate)

    def churn_legacy() -> None:
        for index in range(rounds):
            name = f"legacy-{index}"

            def mutate(data: dict, name: str = name) -> None:
                data.setdefault("legacy", {})[name] = {"enabled": True}

            update_extensions_config(mutate)

    threads = [threading.Thread(target=churn_skills), threading.Thread(target=churn_legacy)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["skills"]) == {f"skill-{index}" for index in range(rounds)}
    assert set(data["legacy"]) == {f"legacy-{index}" for index in range(rounds)}


def test_the_file_lock_excludes_another_process(tmp_path):
    """跨进程咨询锁：另一个进程持锁时，本进程拿不到。"""
    path = tmp_path / "extensions_config.json"
    path.write_text("{}", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError):
            with extensions_config_file_lock(path, timeout=0.3):
                pytest.fail("another process holds the lock")
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    with extensions_config_file_lock(path, timeout=5.0):
        pass


def test_the_lock_file_sits_next_to_the_config(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch)

    with extensions_config_file_lock(path):
        assert (path.parent / f".{path.name}.lock").is_file()


def test_a_failed_replace_leaves_the_original_file_intact(tmp_path, monkeypatch):
    """写到一半失败绝不能让原文件变成截断的 JSON。"""
    path = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "replace", _raise_errno(errno.EIO))

    with pytest.raises(OSError) as raised:
        atomic_write_extensions_config(path, {"skills": {"b": {"enabled": True}}})

    assert raised.value.errno == errno.EIO
    assert json.loads(path.read_text(encoding="utf-8")) == {"skills": {"a": {"enabled": True}}}
    assert _temp_files(path.parent) == []


def test_only_ebusy_degrades_to_an_in_place_overwrite(tmp_path, monkeypatch, caplog):
    path = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "replace", _raise_errno(errno.EBUSY))

    with caplog.at_level(logging.WARNING, logger="agentkit.config.extensions_config"):
        atomic_write_extensions_config(path, {"skills": {"中文技能": {"enabled": False}}})

    assert json.loads(path.read_text(encoding="utf-8"))["skills"]["中文技能"]["enabled"] is False
    assert _temp_files(path.parent) == []
    assert any("EBUSY" in record.getMessage() for record in caplog.records)


def test_other_errno_is_not_swallowed(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "replace", _raise_errno(errno.EACCES))

    with pytest.raises(OSError) as raised:
        atomic_write_extensions_config(path, {"skills": {}})

    assert raised.value.errno == errno.EACCES
    assert json.loads(path.read_text(encoding="utf-8")) == {"skills": {"a": {"enabled": True}}}
    assert _temp_files(path.parent) == []


def test_the_ebusy_warning_is_logged_once_per_path(tmp_path, monkeypatch, caplog):
    path = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "replace", _raise_errno(errno.EBUSY))

    with caplog.at_level(logging.WARNING, logger="agentkit.config.extensions_config"):
        atomic_write_extensions_config(path, {"skills": {"a": {"enabled": True}}})
        atomic_write_extensions_config(path, {"skills": {"a": {"enabled": False}}})

    warnings = [record for record in caplog.records if "EBUSY" in record.getMessage()]
    assert len(warnings) == 1


def test_the_temp_file_is_a_sibling_with_the_documented_name(tmp_path, monkeypatch):
    """同目录临时文件才能保证 os.replace 是一次 rename，而不是跨卷拷贝。"""
    path = _seed(tmp_path, monkeypatch)
    seen: list[str] = []
    real_replace = os.replace

    def spy(source, destination):
        seen.append(str(source))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    atomic_write_extensions_config(path, {"skills": {}})

    assert len(seen) == 1
    temp = Path(seen[0])
    assert temp.parent == path.parent
    assert temp.name.startswith(".extensions_config.json.")
    assert temp.name.endswith(".tmp")


def test_chinese_names_are_written_as_utf8(tmp_path, monkeypatch):
    """ensure_ascii=False：技能名和 description 经常是中文。"""
    path = _seed(tmp_path, monkeypatch)

    atomic_write_extensions_config(path, {"skills": {"中文技能": {"enabled": True}}})

    raw = path.read_bytes()
    assert "中文技能".encode("utf-8") in raw
    assert b"\\u4e2d" not in raw