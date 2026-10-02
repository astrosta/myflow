"""§7.1 JSON filter semantics shared by the memory and SQL thread stores."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentkit.persistence.json_compat import (
    MISSING,
    InvalidMetadataFilterError,
    json_value_matches,
    python_value_matches,
)
from agentkit.persistence.engine import close_engine, get_session_factory, init_engine
from agentkit.persistence.thread_meta.sql import SqlThreadMetaStore

def test_missing_field_does_not_match_null() -> None:
    assert python_value_matches(MISSING, None) is False
    assert python_value_matches(None, None) is True


def test_bool_does_not_match_int() -> None:
    assert python_value_matches(True, True) is True
    assert python_value_matches(1, True) is False
    assert python_value_matches(True, 1) is False
    assert python_value_matches(1, 1) is True


def test_float_filter_accepts_int_and_real_but_not_bool() -> None:
    assert python_value_matches(1, 1.0) is True
    assert python_value_matches(1.0, 1.0) is True
    assert python_value_matches(True, 1.0) is False
    assert python_value_matches(1.5, 1) is False


def test_unsafe_key_or_value_is_rejected() -> None:
    from sqlalchemy import column

    col = column("metadata_json")
    with pytest.raises(InvalidMetadataFilterError, match="unsafe metadata key"):
        json_value_matches(col, "a.b", "x")
    with pytest.raises(InvalidMetadataFilterError, match="unsupported metadata value type"):
        json_value_matches(col, "topic", ["nope"])
    with pytest.raises(InvalidMetadataFilterError, match="signed int64"):
        json_value_matches(col, "n", 2**63)


def test_postgres_int_filter_requires_an_integer_shaped_number() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import Column
    from sqlalchemy.sql.sqltypes import JSON
    from sqlalchemy import select

    metadata = Column("metadata_json", JSON())
    statement = select(metadata).where(json_value_matches(metadata, "n", 1))
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "json_typeof" in sql
    assert "^-?[0-9]+$" in sql
    bool_sql = str(
        select(metadata).where(json_value_matches(metadata, "flag", True)).compile(dialect=postgresql.dialect())
    )
    assert "boolean" in bool_sql
    assert "^-?[0-9]+$" not in bool_sql


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


def test_sqlite_search_uses_the_same_json_semantics(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        store = SqlThreadMetaStore(factory)
        await store.create("t-missing", user_id="alice", metadata={"topic": "中文"})
        await store.create("t-null", user_id="alice", metadata={"topic": None})
        await store.create("t-bool", user_id="alice", metadata={"flag": True, "n": 1})
        await store.create("t-float", user_id="alice", metadata={"n": 1.5})

        nulls = await store.search(metadata={"topic": None}, user_id="alice")
        assert [row["thread_id"] for row in nulls] == ["t-null"]

        flags = await store.search(metadata={"flag": True}, user_id="alice")
        assert [row["thread_id"] for row in flags] == ["t-bool"]
        assert await store.search(metadata={"flag": 1}, user_id="alice") == []

        ones = await store.search(metadata={"n": 1}, user_id="alice")
        assert [row["thread_id"] for row in ones] == ["t-bool"]
        floats = await store.search(metadata={"n": 1.0}, user_id="alice")
        assert {row["thread_id"] for row in floats} == {"t-bool"}
        halves = await store.search(metadata={"n": 1.5}, user_id="alice")
        assert [row["thread_id"] for row in halves] == ["t-float"]
        topics = await store.search(metadata={"topic": "中文"}, user_id="alice")
        assert [row["thread_id"] for row in topics] == ["t-missing"]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(close_engine())
