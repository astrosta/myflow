"""§3.2 schema bootstrap: empty create_all+stamp, known upgrade, otherwise refuse."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentkit.persistence import bootstrap as bootstrap_mod
from agentkit.persistence.bootstrap import (
    SchemaStateError,
    _bootstrap_lock,
    _head_revision,
    bootstrap_schema,
)


def _run(scenario) -> None:
    asyncio.run(scenario())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


async def _engine(directory: Path) -> AsyncEngine:
    return create_async_engine(_sqlite_url(directory))


async def _version(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'")
        )
        if exists is None:
            return None
        return await conn.scalar(text("SELECT version_num FROM alembic_version"))


async def _exec(engine: AsyncEngine, sql: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql))


def test_empty_database_creates_schema_then_stamps_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_create = bootstrap_mod._create_all
    real_stamp = bootstrap_mod._stamp_head
    real_to_thread = asyncio.to_thread

    async def _create(engine: AsyncEngine) -> None:
        order.append("create")
        await real_create(engine)

    async def _stamp(engine: AsyncEngine) -> None:
        order.append("stamp")
        await real_stamp(engine)

    async def _spy(fn, /, *args, **kwargs):
        order.append(getattr(fn, "__name__", ""))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(bootstrap_mod, "_create_all", _create)
    monkeypatch.setattr(bootstrap_mod, "_stamp_head", _stamp)
    monkeypatch.setattr(bootstrap_mod.asyncio, "to_thread", _spy)

    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await bootstrap_schema(engine, backend="sqlite")
            assert order[:2] == ["create", "stamp"]
            assert "stamp" in order
            assert await _version(engine) == _head_revision()
        finally:
            await engine.dispose()

    _run(scenario)


def test_checkpoint_tables_alone_still_count_as_empty(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await _exec(engine, "CREATE TABLE checkpoints (thread_id TEXT)")
            await bootstrap_schema(engine, backend="sqlite")
            assert await _version(engine) == _head_revision()
        finally:
            await engine.dispose()

    _run(scenario)


def test_second_bootstrap_at_head_does_not_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await bootstrap_schema(engine, backend="sqlite")
            await _exec(engine, "CREATE TABLE IF NOT EXISTS threads_meta (thread_id TEXT)")
            calls: list[str] = []

            async def _create(_engine: AsyncEngine) -> None:
                calls.append("create")

            async def _upgrade(_engine: AsyncEngine) -> None:
                calls.append("upgrade")

            monkeypatch.setattr(bootstrap_mod, "_create_all", _create)
            monkeypatch.setattr(bootstrap_mod, "_upgrade_head", _upgrade)
            await bootstrap_schema(engine, backend="sqlite")
            assert calls == []
            assert await _version(engine) == _head_revision()
        finally:
            await engine.dispose()

    _run(scenario)


def test_owned_table_without_alembic_version_refuses_to_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await _exec(engine, "CREATE TABLE threads_meta (thread_id TEXT)")
            with pytest.raises(SchemaStateError, match="not in this build's alembic chain"):
                await bootstrap_schema(engine, backend="sqlite")
            assert await _version(engine) is None
        finally:
            await engine.dispose()

    _run(scenario)


def test_empty_version_table_refuses_to_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await _exec(engine, "CREATE TABLE runs (run_id TEXT)")
            await _exec(
                engine,
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
            )
            with pytest.raises(SchemaStateError, match="revision None"):
                await bootstrap_schema(engine, backend="sqlite")
        finally:
            await engine.dispose()

    _run(scenario)


def test_multiple_version_rows_refuse_to_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await _exec(engine, "CREATE TABLE run_events (id TEXT)")
            await _exec(
                engine,
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
            )
            await _exec(engine, "INSERT INTO alembic_version (version_num) VALUES ('one')")
            await _exec(engine, "INSERT INTO alembic_version (version_num) VALUES ('two')")
            with pytest.raises(SchemaStateError, match="revision None"):
                await bootstrap_schema(engine, backend="sqlite")
        finally:
            await engine.dispose()

    _run(scenario)


def test_unknown_revision_refuses_to_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await _exec(engine, "CREATE TABLE runs (run_id TEXT)")
            await _exec(
                engine,
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
            )
            await _exec(
                engine,
                "INSERT INTO alembic_version (version_num) VALUES ('from-a-newer-build')",
            )
            with pytest.raises(SchemaStateError, match="from-a-newer-build"):
                await bootstrap_schema(engine, backend="sqlite")
            assert await _version(engine) == "from-a-newer-build"
        finally:
            await engine.dispose()

    _run(scenario)


def test_known_non_head_revision_upgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _upgrade(_engine: AsyncEngine) -> None:
        calls.append("upgrade")

    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await bootstrap_schema(engine, backend="sqlite")
            installed = await _version(engine)
            assert installed is not None
            monkeypatch.setattr(bootstrap_mod, "_head_revision", lambda: "0002_future")
            monkeypatch.setattr(
                bootstrap_mod,
                "_local_revisions",
                lambda: frozenset({installed, "0002_future"}),
            )
            monkeypatch.setattr(bootstrap_mod, "_upgrade_head", _upgrade)
            await _exec(engine, "CREATE TABLE IF NOT EXISTS threads_meta (thread_id TEXT)")
            await bootstrap_schema(engine, backend="sqlite")
            assert calls == ["upgrade"]
        finally:
            await engine.dispose()

    _run(scenario)


def test_upgrade_head_runs_alembic_outside_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command

    seen: list[tuple[object, tuple]] = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, /, *args, **kwargs):
        seen.append((fn, args))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(bootstrap_mod.asyncio, "to_thread", _spy)

    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await bootstrap_schema(engine, backend="sqlite")
            seen.clear()
            await bootstrap_mod._upgrade_head(engine)
            assert seen
            assert seen[0][0] is command.upgrade
            assert seen[0][1][1] == "head"
        finally:
            await engine.dispose()

    _run(scenario)


def test_sqlite_bootstraps_do_not_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = 0
    peak = 0
    original = bootstrap_mod._is_empty_database

    async def _slow(engine: AsyncEngine) -> bool:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        try:
            return await original(engine)
        finally:
            current -= 1

    async def _noop(_engine: AsyncEngine) -> None:
        return None

    monkeypatch.setattr(bootstrap_mod, "_is_empty_database", _slow)
    monkeypatch.setattr(bootstrap_mod, "_create_all", _noop)
    monkeypatch.setattr(bootstrap_mod, "_stamp_head", _noop)

    async def scenario() -> None:
        engine = await _engine(tmp_path)
        try:
            await asyncio.gather(
                bootstrap_schema(engine, backend="sqlite"),
                bootstrap_schema(engine, backend="sqlite"),
            )
            assert peak == 1
        finally:
            await engine.dispose()

    _run(scenario)


def test_postgres_lock_covers_the_whole_bootstrap() -> None:
    events: list[str] = []

    class _Conn:
        async def execute(self, statement) -> None:
            events.append(str(statement))

        async def __aenter__(self) -> _Conn:
            events.append("begin")
            return self

        async def __aexit__(self, *_args) -> None:
            events.append("end")

    class _Engine:
        def begin(self) -> _Conn:
            return _Conn()

    async def scenario() -> None:
        async with _bootstrap_lock(_Engine(), backend="postgres"):
            events.append("body")

    _run(scenario)
    assert events == [
        "begin",
        "SELECT pg_advisory_xact_lock(hashtext('agentkit_bootstrap'))",
        "body",
        "end",
    ]
