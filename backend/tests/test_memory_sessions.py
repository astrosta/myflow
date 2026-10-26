"""Task 5: turn capture, de-duplication, and episode-session closing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.capture import build_turn_refs
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.canonical.sessions import (
    SessionService,
    is_explicit_remember,
)
from agentkit.config.memory_config import MemoryConfig
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def make_stack(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "s.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repo = MemoryRepository(async_sessionmaker(engine, expire_on_commit=False))
    return repo, SessionService(repo, MemoryConfig()), FakeOperators()


def test_turn_refs_drop_long_tool_payload_and_keep_only_refs() -> None:
    refs = build_turn_refs(
        [
            HumanMessage(id="u1", content="帮我修一下部署脚本"),
            AIMessage(id="a1", content="好的"),
            HumanMessage(id="u1", content="重复消息不应再次出现"),
        ],
        run_id="r1",
    )
    assert [ref.message_id for ref in refs] == ["u1", "a1"]
    payload = json.dumps([asdict(ref) for ref in refs], ensure_ascii=False)
    assert payload.count("帮我修一下部署脚本") <= 1
    assert all(len(ref.summary) <= 600 for ref in refs)


def test_tool_output_is_not_stored() -> None:
    refs = build_turn_refs(
        [ToolMessage(id="tool1", content="x" * 10_000, tool_call_id="c1")],
        run_id="r1",
    )
    payload = json.dumps([asdict(ref) for ref in refs], ensure_ascii=False)
    assert "x" * 1_000 not in payload
    assert len(payload) < 2_000


def test_explicit_remember_is_detected_without_an_llm() -> None:
    assert is_explicit_remember("记住这次部署的修复步骤") is True
    assert is_explicit_remember("remember this workflow") is True
    assert is_explicit_remember("这次用三行输出") is False


def test_capture_dedupes_and_closes_once(tmp_path) -> None:
    async def scenario():
        _repo, sessions, operators = await make_stack(tmp_path)
        first = await sessions.capture_turn(
            "u1",
            "t1",
            "r1",
            [HumanMessage(id="u1", content="第一轮"), AIMessage(id="a1", content="收到")],
            terminal=False,
        )
        second = await sessions.capture_turn(
            "u1",
            "t1",
            "r1",
            [HumanMessage(id="u1", content="第一轮"), AIMessage(id="a1", content="收到")],
            terminal=True,
        )
        assert first.session_id == second.session_id
        assert second.closed is True
        assert second.job_id is not None
        again = await sessions.close_session("u1", "t1", "terminal")
        assert again is not None and again.job_id == second.job_id
        assert operators.calls == []

    run(scenario())


def test_concurrent_capture_keeps_one_open_session(tmp_path) -> None:
    async def scenario():
        _repo, sessions, _operators = await make_stack(tmp_path)
        await asyncio.gather(
            *[
                sessions.capture_turn(
                    "u1",
                    "t1",
                    f"r{index}",
                    [HumanMessage(id="u1", content="同一轮")],
                    terminal=False,
                )
                for index in range(2)
            ]
        )
        assert await sessions.count_open("u1", "t1") == 1

    run(scenario())


def test_idle_and_max_turn_sweepers_use_config_values(tmp_path) -> None:
    async def scenario():
        _repo, sessions, _operators = await make_stack(tmp_path)
        await sessions.capture_turn(
            "u1", "t1", "r1", [HumanMessage(id="u1", content="x")], terminal=False
        )
        assert await sessions.close_idle_sessions(now=_now() + timedelta(minutes=31)) == 1

        for index in range(41):
            await sessions.capture_turn(
                "u1",
                "t2",
                f"r{index}",
                [HumanMessage(id=f"u{index}", content="x")],
                terminal=False,
            )
        assert await sessions.count_open("u1", "t2") == 1

    run(scenario())


def _now():
    from agentkit.agents.memory.types import utc_now

    return utc_now()
