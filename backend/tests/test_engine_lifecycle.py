"""§3.1 engine lifecycle: memory / sqlite / postgres singletons."""

from __future__ import annotations

import asyncio
import builtins
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agentkit.config.database_config import DatabaseConfig
from agentkit.persistence import engine as engine_mod
from agentkit.persistence.engine import (
    _json_serializer,
    close_engine,
    get_session_factory,
    init_engine,
    init_engine_from_config,
)


def _run(scenario) -> None:
    async def _body() -> None:
        try:
            await scenario()
        finally:
            await close_engine()

    asyncio.run(_body())


def _sqlite_url(directory: Path) -> str:
    return "sqlite+aiosqlite:///" + (directory / "app.db").resolve().as_posix()


@pytest.fixture(autouse=True)
def _isolate_engine() -> None:
    asyncio.run(close_engine())
    yield
    asyncio.run(close_engine())


def test_memory_backend_leaves_session_factory_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_create(*_args, **_kwargs):
        raise AssertionError("memory backend must not create an engine")

    monkeypatch.setattr(engine_mod, "create_async_engine", fail_create)

    async def scenario() -> None:
        await init_engine("memory")
        assert get_session_factory() is None
        assert engine_mod._engine is None

    _run(scenario)


def test_close_engine_disposes_and_clears_the_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    disposed: list[AsyncEngine] = []
    original = AsyncEngine.dispose

    async def _spy(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)
        await original(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", _spy)

    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        live = engine_mod._engine
        assert factory is not None
        assert live is not None
        await close_engine()
        assert disposed == [live]
        assert get_session_factory() is None
        assert engine_mod._engine is None

    _run(scenario)


def test_close_engine_is_safe_when_nothing_was_initialized() -> None:
    async def scenario() -> None:
        await close_engine()
        assert get_session_factory() is None

    _run(scenario)


def test_sqlite_connections_apply_pragmas(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        factory = get_session_factory()
        assert factory is not None
        assert factory.kw["expire_on_commit"] is False

        from sqlalchemy import text

        for _ in range(2):
            async with factory() as session:
                journal = await session.scalar(text("PRAGMA journal_mode"))
                synchronous = await session.scalar(text("PRAGMA synchronous"))
                foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
                busy_timeout = await session.scalar(text("PRAGMA busy_timeout"))
                assert journal == "wal"
                assert synchronous == 1  # NORMAL
                assert foreign_keys == 1
                assert busy_timeout == 30000

    _run(scenario)


def test_sqlite_mkdir_runs_outside_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, tuple, dict]] = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, /, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(engine_mod.asyncio, "to_thread", _spy)
    nested = tmp_path / "missing" / "data"

    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(nested), sqlite_dir=str(nested))
        assert nested.is_dir()
        assert calls
        assert calls[0][0] is os.makedirs
        assert calls[0][1] == (str(nested),)
        assert calls[0][2] == {"exist_ok": True}

    _run(scenario)


def test_blank_sqlite_dir_is_the_current_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def _spy(fn, /, *args, **kwargs):
        seen["fn"] = fn
        seen["args"] = args
        seen["kwargs"] = kwargs

    async def _noop(_engine, *, backend: str) -> None:
        return None

    monkeypatch.setattr(engine_mod.asyncio, "to_thread", _spy)
    monkeypatch.setattr(engine_mod, "create_async_engine", lambda *_a, **_k: _UnusedEngine())
    monkeypatch.setattr(engine_mod.event, "listens_for", lambda *_a, **_k: (lambda fn: fn))
    monkeypatch.setattr("agentkit.persistence.bootstrap.bootstrap_schema", _noop)

    async def scenario() -> None:
        await init_engine("sqlite", url="sqlite+aiosqlite:///ignored.db", sqlite_dir="")
        assert seen["fn"] is os.makedirs
        assert seen["args"] == (".",)
        assert seen["kwargs"] == {"exist_ok": True}

    _run(scenario)


def test_sqlite_engine_does_not_rewrite_the_url_or_take_postgres_pool_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, object] = {}
    real_create = engine_mod.create_async_engine
    url = _sqlite_url(tmp_path)

    def _capture(captured_url: str, **kwargs):
        recorded["url"] = captured_url
        recorded["kwargs"] = kwargs
        return real_create(captured_url, **kwargs)

    monkeypatch.setattr(engine_mod, "create_async_engine", _capture)

    async def scenario() -> None:
        await init_engine(
            "sqlite",
            url=url,
            echo=True,
            pool_size=9,
            sqlite_dir=str(tmp_path),
        )
        assert recorded["url"] == url
        kwargs = recorded["kwargs"]
        assert kwargs == {"echo": True, "json_serializer": _json_serializer}

    _run(scenario)


def test_json_serializer_keeps_non_ascii_and_stringifies_unknown_types() -> None:
    assert _json_serializer({"text": "中文"}) == '{"text": "中文"}'
    payload = {
        "at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "id": UUID(int=1),
    }
    encoded = _json_serializer(payload)
    assert "\\u" not in encoded
    assert "2026-01-02 03:04:05+00:00" in encoded
    assert str(UUID(int=1)) in encoded


def test_postgres_engine_uses_pool_and_schema_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    recorded: dict[str, object] = {}

    def _capture(url: str, **kwargs):
        recorded["url"] = url
        recorded["kwargs"] = kwargs
        engine = _RecordingEngine(order)
        recorded["engine"] = engine
        return engine

    async def _bootstrap(engine, *, backend: str) -> None:
        order.append(f"bootstrap:{backend}")
        assert engine is recorded["engine"]

    monkeypatch.setattr(engine_mod, "create_async_engine", _capture)
    monkeypatch.setattr(engine_mod, "_require_asyncpg", lambda: None)
    monkeypatch.setattr("agentkit.persistence.bootstrap.bootstrap_schema", _bootstrap)

    async def scenario() -> None:
        url = "postgresql+asyncpg://localhost/app"
        await init_engine(
            "postgres",
            url=url,
            echo=True,
            pool_size=7,
            postgres_schema="app_schema",
        )
        assert recorded["url"] == url
        assert recorded["kwargs"] == {
            "echo": True,
            "pool_size": 7,
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "json_serializer": _json_serializer,
            "connect_args": {
                "command_timeout": 30,
                "server_settings": {"search_path": "app_schema"},
            },
        }
        assert order == [
            "CREATE SCHEMA IF NOT EXISTS app_schema",
            "bootstrap:postgres",
        ]
        factory = get_session_factory()
        assert factory is not None
        assert factory.kw["expire_on_commit"] is False

    _run(scenario)


def test_postgres_without_schema_omits_search_path(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def _capture(url: str, **kwargs):
        recorded["kwargs"] = kwargs
        return _RecordingEngine([])

    async def _noop(_engine, *, backend: str) -> None:
        return None

    monkeypatch.setattr(engine_mod, "create_async_engine", _capture)
    monkeypatch.setattr(engine_mod, "_require_asyncpg", lambda: None)
    monkeypatch.setattr("agentkit.persistence.bootstrap.bootstrap_schema", _noop)

    async def scenario() -> None:
        await init_engine("postgres", url="postgresql+asyncpg://localhost/app")
        connect_args = recorded["kwargs"]["connect_args"]
        assert connect_args == {"command_timeout": 30}
        assert engine_mod._engine.statements == []  # type: ignore[union-attr]

    _run(scenario)


def test_unsafe_postgres_schema_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_create(*_args, **_kwargs):
        raise AssertionError("unsafe schema must not create an engine")

    monkeypatch.setattr(engine_mod, "create_async_engine", fail_create)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="postgres_schema"):
            await init_engine(
                "postgres",
                url="postgresql+asyncpg://localhost/app",
                postgres_schema='app";drop schema public;--',
            )

    _run(scenario)


def test_missing_asyncpg_names_the_postgres_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "asyncpg":
            raise ModuleNotFoundError("No module named 'asyncpg'", name="asyncpg")
        return real_import(name, globals, locals, fromlist, level)

    def fail_create(*_args, **_kwargs):
        raise AssertionError("missing asyncpg must not create an engine")

    monkeypatch.setattr(builtins, "__import__", _blocked)
    monkeypatch.setattr(engine_mod, "create_async_engine", fail_create)

    async def scenario() -> None:
        with pytest.raises(ImportError, match=r"uv sync --extra postgres"):
            await init_engine("postgres", url="postgresql+asyncpg://localhost/app")
        assert get_session_factory() is None

    _run(scenario)


def test_bootstrap_failure_clears_the_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(_engine, *, backend: str) -> None:
        raise RuntimeError(f"bootstrap failed:{backend}")

    monkeypatch.setattr("agentkit.persistence.bootstrap.bootstrap_schema", _boom)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="bootstrap failed:sqlite"):
            await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        assert get_session_factory() is None
        assert engine_mod._engine is None

    _run(scenario)


def test_second_init_without_close_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="close_engine"):
            await init_engine("sqlite", url=_sqlite_url(tmp_path), sqlite_dir=str(tmp_path))

    _run(scenario)


def test_unknown_backend_is_rejected() -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="mysql"):
            await init_engine("mysql")  # type: ignore[arg-type]
        assert get_session_factory() is None

    _run(scenario)


def test_init_engine_from_config_forwards_the_dialect_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    async def _capture(backend: str, **kwargs) -> None:
        seen["backend"] = backend
        seen.update(kwargs)

    monkeypatch.setattr(engine_mod, "init_engine", _capture)
    sqlite_config = DatabaseConfig(
        backend="sqlite",
        sqlite_dir=str(tmp_path),
        echo_sql=True,
        pool_size=3,
    )
    postgres_config = DatabaseConfig(
        backend="postgres",
        postgres_url="postgresql://localhost/app",
        postgres_schema="app_schema",
    )
    already = DatabaseConfig(
        backend="postgres",
        postgres_url="postgresql+asyncpg://localhost/app",
    )

    async def scenario() -> None:
        await init_engine_from_config(sqlite_config)
        assert seen == {
            "backend": "sqlite",
            "url": sqlite_config.app_sqlalchemy_url,
            "echo": True,
            "pool_size": 3,
            "sqlite_dir": str(tmp_path),
            "postgres_schema": "",
        }
        assert seen["url"].startswith("sqlite+aiosqlite:///")
        assert "+asyncpg+asyncpg" not in str(seen["url"])

        await init_engine_from_config(postgres_config)
        assert seen["url"] == "postgresql+asyncpg://localhost/app"
        assert seen["postgres_schema"] == "app_schema"

        await init_engine_from_config(already)
        assert seen["url"] == "postgresql+asyncpg://localhost/app"
        short = DatabaseConfig(backend="postgres", postgres_url="postgres://localhost/app")
        assert short.app_sqlalchemy_url == "postgresql+asyncpg://localhost/app"

        await init_engine_from_config(DatabaseConfig(backend="memory"))
        assert seen["backend"] == "memory"

    _run(scenario)


def test_memory_config_does_not_create_an_engine() -> None:
    async def scenario() -> None:
        await init_engine_from_config(DatabaseConfig(backend="memory"))
        assert get_session_factory() is None
        assert engine_mod._engine is None

    _run(scenario)


class _RecordingEngine:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.statements: list[str] = []
        self.sync_engine = object()
        self.disposed = False

    def begin(self):
        return _Begin(self)

    async def dispose(self) -> None:
        self.disposed = True


class _Begin:
    def __init__(self, engine: _RecordingEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _Begin:
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def execute(self, statement) -> None:
        sql = str(statement)
        self._engine.statements.append(sql)
        self._engine.order.append(sql)


class _UnusedEngine:
    sync_engine = object()

    async def dispose(self) -> None:
        return None
