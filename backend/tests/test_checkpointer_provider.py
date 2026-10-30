import builtins
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agentkit.config import CheckpointerConfig, DatabaseConfig
from agentkit.runtime.checkpointer import provider


class _Saver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[_Saver]:
    made: list[_Saver] = []

    def factory(_config: object, _database: object) -> _Saver:
        saver = _Saver()
        made.append(saver)
        return saver

    monkeypatch.setattr(provider, "_make_checkpointer", factory)
    monkeypatch.setattr(
        provider,
        "_resolve_checkpointer_settings",
        lambda: (CheckpointerConfig(), DatabaseConfig()),
    )
    provider.reset_checkpointer()
    yield made
    provider.reset_checkpointer()


def test_get_checkpointer_reuses_the_process_singleton(created: list[_Saver]) -> None:
    first = provider.get_checkpointer()
    second = provider.get_checkpointer()
    assert first is second
    assert created == [first]
    assert not first.closed


def test_reset_closes_the_singleton_and_the_next_get_builds_a_new_one(
    created: list[_Saver],
) -> None:
    first = provider.get_checkpointer()
    provider.reset_checkpointer()
    assert first.closed
    second = provider.get_checkpointer()
    assert second is not first
    assert created == [first, second]
    assert not second.closed


def test_context_builds_and_closes_its_own_saver(created: list[_Saver]) -> None:
    singleton = provider.get_checkpointer()
    with provider.checkpointer_context() as scoped:
        assert scoped is not singleton
        assert not scoped.closed
    assert scoped.closed
    assert not singleton.closed
    assert provider.get_checkpointer() is singleton

    with provider.checkpointer_context() as again:
        assert again is not scoped
    assert again.closed
    assert [singleton, scoped, again] == created[:3]


def test_checkpointer_lock_is_non_reentrant() -> None:
    assert type(provider._checkpointer_lock) is type(threading.Lock())


def test_config_is_resolved_before_the_lock_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    provider.reset_checkpointer()
    order: list[tuple[str, bool]] = []

    def resolve() -> tuple[CheckpointerConfig, DatabaseConfig]:
        order.append(("resolve", provider._checkpointer_lock.locked()))
        return CheckpointerConfig(), DatabaseConfig()

    def make(_config: object, _database: object) -> _Saver:
        order.append(("make", provider._checkpointer_lock.locked()))
        return _Saver()

    monkeypatch.setattr(provider, "_resolve_checkpointer_settings", resolve)
    monkeypatch.setattr(provider, "_make_checkpointer", make)
    try:
        provider.get_checkpointer()
    finally:
        provider.reset_checkpointer()
    assert order == [("resolve", False), ("make", True)]


def test_resolving_config_can_reset_without_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider.reset_checkpointer()
    monkeypatch.setattr(provider, "_make_checkpointer", lambda _config, _database: _Saver())

    def resolve() -> tuple[CheckpointerConfig, DatabaseConfig]:
        provider.reset_checkpointer()
        return CheckpointerConfig(), DatabaseConfig()

    monkeypatch.setattr(provider, "_resolve_checkpointer_settings", resolve)
    finished = threading.Event()

    def run() -> None:
        provider.get_checkpointer()
        finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert finished.wait(2), "get_checkpointer deadlocked while resolving config"
    finally:
        thread.join(timeout=1)
        provider.reset_checkpointer()


def test_concurrent_first_gets_share_one_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    provider.reset_checkpointer()
    made: list[_Saver] = []
    both_resolving = threading.Barrier(2)

    def resolve() -> tuple[CheckpointerConfig, DatabaseConfig]:
        both_resolving.wait(timeout=2)
        return CheckpointerConfig(), DatabaseConfig()

    def make(_config: object, _database: object) -> _Saver:
        saver = _Saver()
        made.append(saver)
        return saver

    monkeypatch.setattr(provider, "_resolve_checkpointer_settings", resolve)
    monkeypatch.setattr(provider, "_make_checkpointer", make)
    results: list[_Saver] = []

    def run() -> None:
        results.append(provider.get_checkpointer())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert len(made) == 1
        assert results[0] is results[1]
    finally:
        provider.reset_checkpointer()


def test_memory_backend_logs_that_it_is_not_persistent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        saver = provider._make_checkpointer(CheckpointerConfig())
    assert isinstance(saver, InMemorySaver)
    assert "not persistent" in caplog.text


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="redis"):
        provider._make_checkpointer(SimpleNamespace(type="redis"))


def test_sqlite_missing_package_includes_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_import(monkeypatch, "langgraph.checkpoint.sqlite")
    with pytest.raises(ImportError, match="uv sync --extra sqlite") as exc_info:
        provider._make_checkpointer(SimpleNamespace(type="sqlite"))
    assert exc_info.value.__cause__ is not None


def test_postgres_missing_package_includes_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_import(monkeypatch, "langgraph.checkpoint.postgres")
    with pytest.raises(ImportError, match="uv sync --extra postgres") as exc_info:
        provider._make_checkpointer(SimpleNamespace(type="postgres"))
    assert exc_info.value.__cause__ is not None


def test_sqlite_checkpointer_creates_the_database_and_closes_it(tmp_path) -> None:
    database = DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path))
    saver = provider._make_checkpointer(SimpleNamespace(type="sqlite"), database)

    assert saver.__class__.__name__ == "SqliteSaver"
    assert Path(database.sqlite_path).exists()
    provider._close_checkpointer(saver)
    with pytest.raises(sqlite3.ProgrammingError):
        saver.conn.execute("SELECT 1")


class _FakeConnection:
    instances: list["_FakeConnection"] = []

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self.instances.append(self)

    @classmethod
    def connect(cls, dsn: str, **kwargs):
        conn = cls()
        conn.dsn = dsn
        conn.kwargs = kwargs
        return conn

    def execute(self, statement: str):
        self.executed.append(statement)

    def close(self) -> None:
        self.closed = True


class _FakePostgresSaver:
    def __init__(self, *, conn) -> None:
        self.conn = conn
        self.setup_called = False

    def setup(self) -> None:
        self.setup_called = True


def test_postgres_checkpointer_connects_sets_search_path_and_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeConnection.instances.clear()
    monkeypatch.setattr(
        provider,
        "_postgres_components",
        lambda: (_FakePostgresSaver, _FakeConnection, object()),
    )
    database = DatabaseConfig(
        backend="postgres",
        postgres_url="postgresql+asyncpg://user:pass@db:5432/myflow",
        postgres_schema="agent",
    )

    saver = provider._make_checkpointer(SimpleNamespace(type="postgres"), database)

    assert isinstance(saver, _FakePostgresSaver)
    assert saver.setup_called
    conn = _FakeConnection.instances[0]
    assert "options=-csearch_path%3Dagent" in conn.dsn
    assert conn.kwargs["autocommit"] is True
    assert conn.kwargs["prepare_threshold"] == 0
    assert conn.kwargs["keepalives"] == 1
    assert conn.executed == ['CREATE SCHEMA IF NOT EXISTS "agent"']

    provider._close_checkpointer(saver)
    assert conn.closed


def test_postgres_checkpointer_closes_the_connection_on_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeConnection.instances.clear()

    class _BrokenSaver(_FakePostgresSaver):
        def setup(self) -> None:
            raise RuntimeError("setup failed")

    monkeypatch.setattr(
        provider,
        "_postgres_components",
        lambda: (_BrokenSaver, _FakeConnection, object()),
    )
    database = DatabaseConfig(
        backend="postgres",
        postgres_url="postgresql://user:pass@db:5432/myflow",
    )

    with pytest.raises(RuntimeError, match="setup failed"):
        provider._make_checkpointer(SimpleNamespace(type="postgres"), database)
    assert _FakeConnection.instances[0].closed


def test_postgres_checkpointer_requires_a_postgres_database() -> None:
    with pytest.raises(ValueError, match="database.backend"):
        provider._make_checkpointer(
            SimpleNamespace(type="postgres"),
            DatabaseConfig(backend="sqlite"),
        )


def test_postgres_checkpointer_requires_a_url() -> None:
    with pytest.raises(ValueError, match="database.url"):
        provider._make_checkpointer(
            SimpleNamespace(type="postgres"),
            DatabaseConfig(backend="postgres"),
        )


def _block_import(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module_name:
            raise ImportError(f"missing {module_name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)
