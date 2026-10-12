"""M7 §2.2：锁是 threading.Lock，在 to_thread 的 worker 里拿，取消 task 不放锁。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from agentkit.config.extensions_config import (
    EXTENSIONS_CONFIG_ENV_NAMES,
    extensions_config_write_lock,
    update_extensions_config,
)


@pytest.fixture(autouse=True)
def _config_file(tmp_path, monkeypatch) -> Path:
    for name in EXTENSIONS_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "extensions_config.json"
    path.write_text(json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    return path


def test_the_process_lock_is_a_threading_lock():
    assert isinstance(extensions_config_write_lock, type(threading.Lock()))


def test_the_lock_is_taken_inside_the_worker_thread(_config_file):
    seen: dict[str, object] = {}

    def mutate(data: dict) -> None:
        seen["thread"] = threading.current_thread()
        seen["locked"] = extensions_config_write_lock.locked()
        data["skills"] = {"pdf-processing": {"enabled": True}}

    async def main() -> None:
        seen["loop_thread"] = threading.current_thread()
        await asyncio.to_thread(update_extensions_config, mutate)
        seen["locked_after"] = extensions_config_write_lock.locked()

    asyncio.run(main())

    assert seen["thread"] is not seen["loop_thread"]
    assert seen["locked"] is True
    assert seen["locked_after"] is False
    assert json.loads(_config_file.read_text(encoding="utf-8"))["skills"] == {
        "pdf-processing": {"enabled": True}
    }


def test_cancelling_the_awaiting_task_does_not_release_the_lock(_config_file):
    """asyncio.Lock 包着 to_thread 时 task 一取消锁就放，后台线程还在写，第二个 writer 闯进去。"""
    entered = threading.Event()
    release = threading.Event()

    def mutate(data: dict) -> None:
        entered.set()
        assert release.wait(timeout=10)
        data["skills"] = {"slow": {"enabled": True}}

    async def main() -> None:
        task = asyncio.create_task(asyncio.to_thread(update_extensions_config, mutate))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # task 已经取消，worker 还在临界区里：锁必须还在
        assert extensions_config_write_lock.locked() is True
        release.set()
        for _ in range(1000):
            if not extensions_config_write_lock.locked():
                break
            await asyncio.sleep(0.01)
        assert extensions_config_write_lock.locked() is False

    asyncio.run(main())

    assert json.loads(_config_file.read_text(encoding="utf-8"))["skills"] == {
        "slow": {"enabled": True}
    }


def test_a_raising_mutator_writes_nothing(_config_file):
    def mutate(data: dict) -> None:
        data["skills"] = {"half": {"enabled": True}}
        raise RuntimeError("mcp_server_not_found")

    with pytest.raises(RuntimeError):
        update_extensions_config(mutate)

    assert json.loads(_config_file.read_text(encoding="utf-8")) == {"mcpServers": {}, "skills": {}}
