"""M7 §2.3/§5.1：工具懒加载的 single-flight 与内容签名失效。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agentkit.mcp import cache


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "extensions_config.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "npx"}}}), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    cache.reset_local_mcp_cache()
    yield path
    cache.reset_local_mcp_cache()


def _tools() -> list:
    return ["tool-a", "tool-b"]


def _loaded(*tools: str, failed: tuple[str, ...] = ()) -> "cache._LoadResult":
    return cache._LoadResult(tools=list(tools), failed=list(failed))


def test_tools_are_loaded_once_per_signature(config_path, monkeypatch):
    loads: list[int] = []

    def fake_load():
        loads.append(1)
        return _loaded(*_tools())

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)

    assert cache.get_cached_mcp_tools() == _tools()
    assert cache.get_cached_mcp_tools() == _tools()
    assert len(loads) == 1


def test_a_content_change_reloads_and_retires_sessions(config_path, monkeypatch):
    loads: list[int] = []

    def fake_load():
        loads.append(1)
        return _loaded(f"tool-{len(loads)}")

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)
    retired: list[str] = []
    unregister = cache.register_session_retirer(retired.append)
    try:
        generation = cache.mcp_cache_generation()
        assert cache.get_cached_mcp_tools() == ["tool-1"]

        config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        assert cache.get_cached_mcp_tools() == ["tool-2"]
        assert cache.mcp_cache_generation() > generation
        assert retired  # 内容签名失效 → 走正常的会话退役路径
    finally:
        unregister()


def test_a_change_during_the_load_discards_the_result(config_path, monkeypatch):
    """加载中途签名变化必须退役会话并重来，不能把旧结果留在缓存里。"""
    loads: list[int] = []
    retired: list[str] = []

    def fake_load():
        loads.append(1)
        if len(loads) == 1:
            config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            return _loaded("stale")
        return _loaded("fresh")

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)
    unregister = cache.register_session_retirer(retired.append)
    try:
        assert cache.get_cached_mcp_tools() == ["fresh"]
    finally:
        unregister()

    assert len(loads) == 2
    assert retired  # 丢弃结果 = 走和正常失效一样的会话退役
    assert cache.get_cached_mcp_tools() == ["fresh"]


def test_concurrent_callers_share_one_load(config_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    loads: list[int] = []

    def fake_load():
        loads.append(1)
        started.set()
        release.wait(timeout=10)
        return _loaded(*_tools())

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)
    results: list[list] = []

    def worker():
        results.append(cache.get_cached_mcp_tools())

    first = threading.Thread(target=worker)
    first.start()
    assert started.wait(timeout=10)
    second = threading.Thread(target=worker)
    second.start()
    time.sleep(0.1)  # 第二个线程应该在等第一个
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(loads) == 1
    assert results == [_tools(), _tools()]


def test_an_unconfigured_deployment_returns_no_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("MYFLOW_EXTENSIONS_CONFIG_PATH", raising=False)
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    cache.reset_local_mcp_cache()

    assert cache.get_cached_mcp_tools() == []


def test_a_vanished_explicit_path_is_unconfigured_on_the_hot_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(tmp_path / "gone.json"))
    cache.reset_local_mcp_cache()

    assert cache.get_cached_mcp_tools() == []  # 热路径不打 500


def test_a_failed_load_is_reported_as_no_tools(config_path, monkeypatch):
    def boom():
        raise RuntimeError("adapter is missing")

    monkeypatch.setattr(cache, "_load_tools_from_scratch", boom)

    assert cache.get_cached_mcp_tools() == []


def test_reset_clears_the_cache_and_bumps_the_generation(config_path, monkeypatch):
    monkeypatch.setattr(cache, "_load_tools_from_scratch", lambda: _loaded(*_tools()))
    assert cache.get_cached_mcp_tools() == _tools()
    generation = cache.mcp_cache_generation()

    cache.reset_local_mcp_cache()

    assert cache.mcp_cache_generation() == generation + 1
    assert cache._cache is None

def test_a_failed_server_is_retried_after_the_window(config_path, monkeypatch):
    """一次抽风不该把一整个进程钉死：签名没变也要在窗口之后重连。"""
    loads: list[int] = []

    def fake_load():
        loads.append(1)
        if len(loads) == 1:
            return _loaded("healthy", failed=("broken",))
        return _loaded("healthy", "recovered")

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)
    monkeypatch.setattr(cache, "RETRY_AFTER_SECONDS", 0.0)

    assert cache.get_cached_mcp_tools() == ["healthy"]
    assert cache.get_cached_mcp_tools() == ["healthy", "recovered"]
    assert len(loads) == 2


def test_a_broken_loader_does_not_wedge_the_cache(config_path, monkeypatch):
    """加载之后的任何异常都不能把 _loading 留在 True 上（waiter 会干等到超时）。"""
    calls: list[int] = []

    def fake_load():
        calls.append(1)
        return object()  # 不是 _LoadResult：后面的 result.tools 会炸

    monkeypatch.setattr(cache, "_load_tools_from_scratch", fake_load)

    with pytest.raises(AttributeError):
        cache.get_cached_mcp_tools()

    assert cache._loading is False
    monkeypatch.setattr(cache, "_load_tools_from_scratch", lambda: _loaded("ok"))
    assert cache.get_cached_mcp_tools() == ["ok"]
