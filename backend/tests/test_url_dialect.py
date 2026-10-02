"""§6.5 checkpointer DSNs use libpq, not the ORM's asyncpg URL."""

from __future__ import annotations

import pytest

from agentkit.config.database_config import (
    DatabaseConfig,
    dsn_with_search_path,
    normalize_libpq_dsn,
)
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS


def test_asyncpg_and_postgres_alias_become_a_libpq_dsn() -> None:
    assert normalize_libpq_dsn("postgresql+asyncpg://localhost/app") == "postgresql://localhost/app"
    assert normalize_libpq_dsn("postgres://localhost/app") == "postgresql://localhost/app"
    assert normalize_libpq_dsn("postgresql://localhost/app") == "postgresql://localhost/app"
    config = DatabaseConfig(
        backend="postgres",
        postgres_url="postgresql+asyncpg://localhost/app",
        postgres_schema="app",
    )
    dsn = config.checkpointer_dsn
    assert "+asyncpg" not in dsn
    assert "postgresql://localhost/app" in dsn
    assert "search_path" in dsn and "app" in dsn


def test_yaml_url_alias_is_the_postgres_url() -> None:
    config = DatabaseConfig.model_validate({"backend": "postgres", "url": "postgresql://localhost/app"})
    assert config.postgres_url == "postgresql://localhost/app"
    assert config.app_sqlalchemy_url == "postgresql+asyncpg://localhost/app"


def test_unsafe_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="postgres_schema"):
        dsn_with_search_path("postgresql://localhost/app", "app;drop")


def test_checkpoint_channel_mode_is_frozen_at_startup() -> None:
    assert "database.checkpoint_channel_mode" in STARTUP_ONLY_FIELDS
    assert "database.backend" in STARTUP_ONLY_FIELDS
