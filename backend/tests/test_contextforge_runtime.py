import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.cache import invalidate_contextforge_cache
from agentkit.contextforge.runtime import ContextForgeRuntime


class FakeAdmin:
    async def list_tools(self, **kwargs):
        return []

    async def aclose(self):
        return None


def _tool() -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    return StructuredTool.from_function(_run, name="demo", description="demo")


@pytest.mark.asyncio
async def test_refresh_sets_snapshot_and_ready_status(monkeypatch) -> None:
    invalidate_contextforge_cache("test")
    config = ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="",
        tool_loading="eager",
        search={"enabled": False},
    )
    runtime = ContextForgeRuntime(config, admin=FakeAdmin(), index=None)

    async def fake_load(config):
        return [_tool()]

    monkeypatch.setattr("agentkit.contextforge.runtime.load_contextforge_tools", fake_load)
    stats = await runtime.refresh_and_sync(reason="test")
    assert stats.scanned == 1
    assert runtime.status()["sync"] == "ready"
    assert runtime.status()["indexed_tools"] == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_index_failure_keeps_the_snapshot(monkeypatch) -> None:
    invalidate_contextforge_cache("test")
    config = ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="",
        tool_loading="eager",
        search={"enabled": False},
    )

    class FailingIndex:
        async def sync(self, entries, *, embedder, full=False):
            raise RuntimeError("milvus down")

        async def ensure_collection(self, dimensions=None):
            return None

        async def aclose(self):
            return None

    runtime = ContextForgeRuntime(
        config,
        admin=FakeAdmin(),
        index=FailingIndex(),
        embedder=SimpleNamespace(embed_documents=None),
    )

    async def fake_load(config):
        return [_tool()]

    monkeypatch.setattr("agentkit.contextforge.runtime.load_contextforge_tools", fake_load)
    with pytest.raises(RuntimeError):
        await runtime.refresh_and_sync(reason="test")
    assert runtime.status()["indexed_tools"] == 1
    assert runtime.status()["sync"] == "error"
    await runtime.stop()

@pytest.mark.asyncio
async def test_admin_refresh_can_be_configured_synchronous(monkeypatch) -> None:
    invalidate_contextforge_cache("test-sync")
    config = ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="",
        tool_loading="eager",
        search={"enabled": False},
        sync={"admin_refresh_async": False},
    )
    runtime = ContextForgeRuntime(config, admin=FakeAdmin(), index=None)

    async def fake_load(config):
        return [_tool()]

    monkeypatch.setattr("agentkit.contextforge.runtime.load_contextforge_tools", fake_load)
    await runtime.request_refresh("admin")
    assert runtime.status()["sync"] == "ready"
    assert runtime.status()["indexed_tools"] == 1
    await runtime.stop()

@pytest.mark.asyncio
async def test_synchronous_admin_refresh_keeps_the_successful_write(monkeypatch) -> None:
    invalidate_contextforge_cache("test-sync-failure")
    config = ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token="",
        tool_loading="eager",
        search={"enabled": False},
        sync={"admin_refresh_async": False},
    )
    runtime = ContextForgeRuntime(config, admin=FakeAdmin(), index=None)

    async def failing_load(config):
        raise RuntimeError("contextforge down")

    monkeypatch.setattr("agentkit.contextforge.runtime.load_contextforge_tools", failing_load)
    await runtime.request_refresh("admin")
    assert runtime.status()["sync"] == "degraded"
    assert runtime.status()["last_error"] == "RuntimeError"
    await runtime.stop()
