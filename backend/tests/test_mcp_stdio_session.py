"""M7 §5.3：stdio 会话池 —— owner task、精确驱逐、不自动重放。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool
from mcp.types import CallToolResult, TextContent

from agentkit.mcp import session_pool as pool_module
from agentkit.mcp.session_pool import McpStdioSessionPool, is_disconnect_error, pool_key
from agentkit.tools.mcp_metadata import mcp_tool_meta


class FakeSession:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.entered_in = None
        self.exited_in = None
        self.closed = False
        self.error: BaseException | None = None

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        # 真的 CallToolResult：适配器的内容块转换要吃它
        return CallToolResult(content=[TextContent(type="text", text=f"{self.name}:{name}")])


class FakeConnect:
    """假的 create_session：记录 enter/exit 发生在哪个 task。"""

    def __init__(self, session: FakeSession, delay: float = 0.0) -> None:
        self.session = session
        self.delay = delay

    async def __aenter__(self) -> FakeSession:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.session.entered_in = asyncio.current_task()
        return self.session

    async def __aexit__(self, *exc) -> None:
        self.session.exited_in = asyncio.current_task()
        self.session.closed = True


def _pool(*, max_sessions: int = 8, session_init_timeout: float | None = 60.0):
    """每次 connect 都建一个新假 session，并记在 created 里。"""
    created: list[FakeSession] = []

    def connect(params):
        session = FakeSession(f"s{len(created) + 1}")
        created.append(session)
        return FakeConnect(session, delay=params.get("delay", 0.0))

    pool = McpStdioSessionPool(
        connect=connect, max_sessions=max_sessions, session_init_timeout=session_init_timeout
    )
    return pool, created


def test_key_is_server_plus_user_and_thread():
    assert pool_key("demo", user_id="u1", thread_id="t1") == ("demo", "u1:t1")
    assert pool_key("demo", user_id=None, thread_id=None) == ("demo", "None:None")


def test_sessions_are_isolated_by_user_and_thread():
    pool, _ = _pool()

    async def run():
        first = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        again = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        other_user = await pool.acquire("demo", user_id="u2", thread_id="t1", params={})
        other_thread = await pool.acquire("demo", user_id="u1", thread_id="t2", params={})
        other_server = await pool.acquire("other", user_id="u1", thread_id="t1", params={})
        assert first is again
        assert len({id(first), id(other_user), id(other_thread), id(other_server)}) == 4

    asyncio.run(run())


def test_the_owner_task_enters_and_exits_the_same_task():
    """ClientSession 建立在 anyio task group 上，__aenter__ / __aexit__ 必须是同一 task。"""
    pool, _ = _pool()

    async def run():
        caller = asyncio.current_task()
        session = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        await session.call_tool("ping")  # 调用方不是 owner，但可以用
        await pool.aclose()
        assert session.entered_in is session.exited_in  # owner 自己进出
        assert session.entered_in is not caller

    asyncio.run(run())


def test_a_full_pool_evicts_the_least_recently_used():
    pool, created = _pool(max_sessions=1)

    async def run():
        first = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        await pool.acquire("demo", user_id="u2", thread_id="t1", params={})
        await asyncio.sleep(0.05)  # owner 收到 close event 后自己退出
        assert first.closed is True
        assert pool.session_count() == 1
        assert created[1].closed is False

    asyncio.run(run())


def test_acquiring_again_touches_the_lru_order():
    pool, created = _pool(max_sessions=2)

    async def run():
        first = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        second = await pool.acquire("demo", user_id="u2", thread_id="t1", params={})
        await pool.acquire("demo", user_id="u1", thread_id="t1", params={})  # u1 变最近使用
        await pool.acquire("demo", user_id="u3", thread_id="t1", params={})  # 触发驱逐
        await asyncio.sleep(0.05)
        assert first.closed is False  # u1 刚被 touch 过
        assert second.closed is True  # u2 才是最久未用的

    asyncio.run(run())
    assert len(created) == 3


def test_a_disconnect_evicts_only_its_own_session():
    pool, _ = _pool()

    async def run():
        healthy = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        broken = await pool.acquire("demo", user_id="u2", thread_id="t1", params={})
        evicted = pool.evict_on_disconnect(pool_key("demo", user_id="u2", thread_id="t1"), broken)
        await asyncio.sleep(0.05)
        assert evicted is True
        assert broken.closed is True
        assert healthy.closed is False
        assert pool.session_count() == 1

    asyncio.run(run())


def test_a_late_error_cannot_kill_the_replacement_session():
    pool, _ = _pool()

    async def run():
        old = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        pool.evict_on_disconnect(pool_key("demo", user_id="u1", thread_id="t1"), old)
        await asyncio.sleep(0.05)
        fresh = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        late = pool.evict_on_disconnect(pool_key("demo", user_id="u1", thread_id="t1"), old)
        assert fresh is not old
        assert late is False  # 晚到的旧错误：注册表里的对象已经不是它了
        assert fresh.closed is False
        assert pool.session_count() == 1

    asyncio.run(run())


def test_retire_signals_every_session_and_empties_the_pool():
    pool, _ = _pool()

    async def run():
        first = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        second = await pool.acquire("other", user_id="u1", thread_id="t1", params={})
        pool.retire("config_changed")
        await asyncio.sleep(0.05)
        assert pool.session_count() == 0
        assert first.closed is True and second.closed is True

    asyncio.run(run())


def test_an_init_timeout_cleans_up_the_entry():
    pool, _ = _pool(session_init_timeout=0.05)

    async def run():
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await pool.acquire(
                "demo", user_id="u1", thread_id="t1", params={"delay": 30.0}
            )
        assert pool.session_count() == 0

    asyncio.run(run())


def test_a_connect_failure_reaches_the_caller_and_leaves_no_entry():
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("npx not found")

        async def __aexit__(self, *exc):
            return None

    pool = McpStdioSessionPool(connect=lambda params: Boom())

    async def run():
        with pytest.raises(RuntimeError, match="npx not found"):
            await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        assert pool.session_count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,expected",
    [
        (RuntimeError("Connection closed"), True),
        (RuntimeError("ClosedResourceError: stream is closed"), True),
        (RuntimeError("EndOfStream"), True),
        (RuntimeError("Connection reset by peer"), True),
        (RuntimeError("protocol timeout"), False),
        (RuntimeError("tool returned isError=true"), False),
        (ValueError("missing required secret(s): TOKEN"), False),
    ],
)
def test_only_explicit_disconnects_count(error, expected):
    assert is_disconnect_error(error) is expected


def _stdio_tool(name: str = "search") -> StructuredTool:
    def run(value: str = "x") -> str:
        """A fake MCP tool."""
        return value

    return StructuredTool.from_function(run, name=name, description="fake")


def _wrapped(monkeypatch, pool, *, timeout=5.0):
    from agentkit.mcp import tools as mcp_tools

    monkeypatch.setattr(mcp_tools, "get_stdio_pool", lambda: pool)
    tool = mcp_tools.wrap_stdio_tool(
        _stdio_tool(),
        server="demo",
        original_name="search",
        params={"transport": "stdio"},
        tool_call_timeout=timeout,
    )
    return mcp_tools, tool


def test_the_stdio_wrapper_uses_the_pool_and_the_original_name(monkeypatch):
    pool, _ = _pool()
    _, tool = _wrapped(monkeypatch, pool)
    runtime = SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"}, config={})

    async def run():
        result = await tool.coroutine(runtime=runtime, value="hi")
        session = pool._sessions[("demo", "u1:t1")].session
        assert session.calls == [("search", {"value": "hi"})]  # 调用用的是**原始**名字
        assert "search" in str(result)  # content_and_artifact：内容块里能看到结果

    asyncio.run(run())
    assert tool.name == "search"


def test_a_disconnect_evicts_and_is_never_replayed(monkeypatch):
    pool, _ = _pool()
    _, tool = _wrapped(monkeypatch, pool)
    runtime = SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"}, config={})

    async def run():
        session = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        session.error = RuntimeError("Connection closed")
        with pytest.raises(RuntimeError):
            await tool.coroutine(runtime=runtime, value="hi")
        assert session.calls == [("search", {"value": "hi"})]  # 只试了一次，绝不自动重放
        await asyncio.sleep(0.05)
        assert pool.session_count() == 0  # 失败的那一个被驱逐

    asyncio.run(run())


def test_a_protocol_timeout_does_not_evict_the_session(monkeypatch):
    pool, _ = _pool()
    _, tool = _wrapped(monkeypatch, pool)
    runtime = SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"}, config={})

    async def run():
        session = await pool.acquire("demo", user_id="u1", thread_id="t1", params={})
        session.error = TimeoutError("protocol timeout")
        with pytest.raises(TimeoutError):
            await tool.coroutine(runtime=runtime, value="hi")
        assert pool.session_count() == 1  # 健康会话留着
        assert session.closed is False

    asyncio.run(run())


def test_the_wrapper_keeps_the_mcp_metadata(monkeypatch):
    pool, _ = _pool()
    _, tool = _wrapped(monkeypatch, pool, timeout=None)
    from agentkit.tools.mcp_metadata import tag_mcp_tool

    tag_mcp_tool(tool, server="demo", original_name="search")

    assert mcp_tool_meta(tool).server == "demo"
    assert mcp_tool_meta(tool).original_name == "search"


def test_get_stdio_pool_is_per_event_loop(monkeypatch):
    monkeypatch.setattr(pool_module, "_pools", pool_module.weakref.WeakKeyDictionary())
    monkeypatch.setattr(
        "agentkit.mcp.cache.register_session_retirer", lambda callback: (lambda: None)
    )

    async def current():
        return pool_module.get_stdio_pool()

    first = asyncio.run(current())
    second = asyncio.run(current())

    assert first is not second  # 不同 loop 不同池：session 绝不跨 loop 复用
