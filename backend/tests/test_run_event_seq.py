"""§5 RunEventStore: per-thread seq, cursors, batch order, and put_if_absent."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from agentkit.config.run_events_config import RunEventsConfig
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.runtime.events.catalog import LLM_HUMAN_INPUT, RUN_START_EVENT
from agentkit.persistence.events import (
    DbRunEventStore,
    JsonlRunEventStore,
    MemoryRunEventStore,
    make_run_event_store,
)
from agentkit.runtime.user_context import reset_current_user, set_current_user

_PUBLIC = {
    "thread_id",
    "run_id",
    "event_type",
    "category",
    "content",
    "metadata",
    "seq",
    "created_at",
}


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def _event(
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    definition=LLM_HUMAN_INPUT,
    content: object = "hi",
    **extra: object,
) -> dict:
    event = {
        "thread_id": thread_id,
        "run_id": run_id,
        "event_type": definition.event_type,
        "category": definition.category,
        "content": content,
        "metadata": {},
    }
    event.update(extra)
    return event


async def _open(backend: str, directory: Path):
    if backend == "memory":
        return MemoryRunEventStore()
    if backend == "jsonl":
        return JsonlRunEventStore(directory / "ledger")
    await init_engine("sqlite", url=_sqlite_url(directory), sqlite_dir=str(directory))
    factory = get_session_factory()
    assert factory is not None
    return DbRunEventStore(factory)


def _assert_public(record: dict) -> None:
    assert _PUBLIC <= record.keys()
    assert isinstance(record["seq"], int)
    assert isinstance(record["metadata"], dict)
    assert isinstance(record["created_at"], str) and record["created_at"].endswith("Z")
    assert isinstance(record["content"], (str, dict))


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_seq_is_per_thread_and_put_is_visible(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        first = await store.put_batch([_event(thread_id="alpha", content={"text": "你好"})])
        second = await store.put_batch([_event(thread_id="beta", content="plain")])
        assert await store.put(_event(thread_id="alpha", run_id="run-2", content="next")) is None
        assert first[0]["seq"] == 1
        assert second[0]["seq"] == 1
        assert first[0]["content"] == {"text": "你好"}
        _assert_public(first[0])
        visible = await store.list_events("alpha", "run-1")
        assert [row["seq"] for row in visible] == [1]
        assert visible[0]["content"] == {"text": "你好"}
        later = await store.list_events("alpha", "run-2")
        assert [row["seq"] for row in later] == [2]

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_concurrent_puts_do_not_reuse_seq(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)

        async def once(index: int) -> None:
            await store.put(_event(run_id="shared", content=str(index)))

        await asyncio.gather(*(once(index) for index in range(12)))
        rows = await store.list_events("thread-1", "shared")
        assert sorted(row["seq"] for row in rows) == list(range(1, 13))
        assert len({row["seq"] for row in rows}) == 12

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_message_cursors_are_ascending_in_both_directions(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        await store.put_batch(
            [
                _event(definition=RUN_START_EVENT, content="start"),
                _event(content="a"),
                _event(content="b"),
                _event(definition=RUN_START_EVENT, content="mid"),
                _event(content="c"),
                _event(content="d"),
            ]
        )
        latest = await store.list_messages("thread-1", limit=2)
        assert [row["content"] for row in latest] == ["c", "d"]
        assert [row["seq"] for row in latest] == [5, 6]
        older = await store.list_messages("thread-1", limit=2, before_seq=5)
        assert [row["content"] for row in older] == ["a", "b"]
        assert [row["seq"] for row in older] == [2, 3]
        newer = await store.list_messages("thread-1", limit=2, after_seq=3)
        assert [row["content"] for row in newer] == ["c", "d"]
        assert newer[0]["seq"] < newer[1]["seq"]
        traces = await store.list_events(
            "thread-1",
            "run-1",
            event_types=[RUN_START_EVENT.event_type],
            after_seq=1,
        )
        assert [row["seq"] for row in traces] == [4]
        assert all(row["category"] == "trace" for row in traces)

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_put_batch_assigns_consecutive_seq_and_ignores_caller_seq(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        assert await store.put_batch([]) == []
        rows = await store.put_batch(
            [
                _event(content="one", seq=99, created_at="1999-01-01T00:00:00Z"),
                _event(content="two"),
                _event(content="three"),
            ]
        )
        assert [row["content"] for row in rows] == ["one", "two", "three"]
        assert [row["seq"] for row in rows] == [1, 2, 3]
        assert all(row["created_at"] != "1999-01-01T00:00:00Z" for row in rows)
        with pytest.raises(ValueError, match="thread_id"):
            await store.put_batch([_event(thread_id="alpha"), _event(thread_id="beta")])
        assert [row["seq"] for row in await store.list_events("thread-1", "run-1")] == [1, 2, 3]

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_put_if_absent_second_call_is_not_created(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        first, created = await store.put_if_absent(_event(content="receipt"))
        again, created_again = await store.put_if_absent(_event(content="retry"))
        other, other_created = await store.put_if_absent(_event(run_id="run-2", content="other"))
        assert created is True and created_again is False and other_created is True
        assert again["seq"] == first["seq"]
        assert again["content"] == "receipt"
        assert other["seq"] == first["seq"] + 1
        rows = await store.list_events("thread-1", "run-1")
        assert len(rows) == 1

    _run(scenario)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "jsonl"])
def test_user_id_is_soft_and_an_explicit_value_wins(backend: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open(backend, tmp_path)
        token = set_current_user("alice")
        try:
            from_context = (await store.put_batch([_event(run_id="run-ctx")]))[0]
            explicit = (await store.put_batch([_event(run_id="run-explicit", user_id="bob")]))[0]
            cleared = (await store.put_batch([_event(run_id="run-none", user_id=None)]))[0]
        finally:
            reset_current_user(token)
        missing = (await store.put_batch([_event(run_id="run-missing")]))[0]
        assert from_context["user_id"] == "alice"
        assert explicit["user_id"] == "bob"
        assert cleared.get("user_id") is None
        assert missing.get("user_id") is None

    _run(scenario)


def test_db_truncates_trace_bytes_and_leaves_messages_whole(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = DbRunEventStore(factory, max_trace_content=4)
        rows = await store.put_batch(
            [
                _event(definition=RUN_START_EVENT, content="中文中文"),
                _event(content="中文中文"),
                _event(definition=RUN_START_EVENT, run_id="run-2", content={"text": "中" * 20}),
            ]
        )
        assert rows[0]["content"] == "中"
        assert rows[0]["metadata"]["content_truncated"] is True
        assert "content_is_json" not in rows[0]["metadata"]
        assert rows[1]["content"] == "中文中文"
        assert "content_truncated" not in rows[1]["metadata"]
        assert isinstance(rows[2]["content"], str)
        assert rows[2]["metadata"]["content_truncated"] is True
        assert rows[2]["metadata"].get("content_is_json") is not True
        full = DbRunEventStore(factory)
        short = await full.put_batch(
            [_event(definition=RUN_START_EVENT, run_id="run-3", content={"text": "你好"})]
        )
        assert short[0]["content"] == {"text": "你好"}
        assert short[0]["metadata"]["content_is_json"] is True

    _run(scenario)


def test_overlong_event_type_is_rejected_before_insert(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _open("sqlite", tmp_path)
        with pytest.raises(ValueError, match="event_type"):
            await store.put_batch([_event(event_type="x" * 33, category="trace")])
        assert await store.list_events("thread-1", "run-1") == []

    _run(scenario)


def test_factory_keeps_run_events_independent_of_the_database(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        memory = make_run_event_store(RunEventsConfig())
        assert isinstance(memory, MemoryRunEventStore)
        db_store = make_run_event_store(RunEventsConfig(backend="db", max_trace_content=8))
        assert isinstance(db_store, DbRunEventStore)
        assert db_store._max_trace_content == 8

    _run(scenario)

    with pytest.raises(RuntimeError, match="db"):
        make_run_event_store(RunEventsConfig(backend="memory"), gateway_workers=2)
    with pytest.raises(RuntimeError, match="db"):
        make_run_event_store(
            RunEventsConfig(backend="jsonl"),
            gateway_workers=2,
            jsonl_base_dir=str(tmp_path),
        )
    with pytest.raises(RuntimeError, match="db"):
        make_run_event_store(RunEventsConfig(backend="db"), gateway_workers=2, session_factory=None)

    caplog.set_level(logging.WARNING)
    fallback = make_run_event_store(RunEventsConfig(backend="db"), session_factory=None)
    assert isinstance(fallback, MemoryRunEventStore)
    assert "memory" in caplog.text.lower()
    jsonl = make_run_event_store(
        RunEventsConfig(backend="jsonl"),
        session_factory=None,
        jsonl_base_dir=str(tmp_path / "jsonl"),
    )
    assert isinstance(jsonl, JsonlRunEventStore)
