"""One database section drives the ORM URL and the checkpointer DSN."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LIBPQ_PREFIXES = (
    "postgresql+asyncpg://",
    "postgres+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgres://",
)


def normalize_libpq_dsn(url: str) -> str:
    """Strip SQLAlchemy driver suffixes. libpq only understands ``postgresql://``."""
    for prefix in _LIBPQ_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def dsn_with_search_path(dsn: str, schema: str) -> str:
    """Pin ``search_path`` on the libpq DSN. An empty schema leaves the DSN unchanged."""
    if not schema:
        return dsn
    if not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(
            "database.postgres_schema must match [A-Za-z_][A-Za-z0-9_]* "
            f"(got {schema!r})"
        )
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(parts._replace(query=urlencode(query)))


class DatabaseConfig(BaseModel):
    """`database.backend` picks memory, sqlite, or postgres.

    `init_engine` receives `app_sqlalchemy_url` already dialect-qualified.
    Callers must not append `+asyncpg` / `+aiosqlite` a second time.
    The example YAML still spells the Postgres URL as ``url``.
    """

    model_config = ConfigDict(populate_by_name=True)

    backend: Literal["memory", "sqlite", "postgres"] = "memory"
    checkpoint_channel_mode: Literal["full", "delta"] = "full"
    sqlite_dir: str = ".myflow/data"
    postgres_url: str = Field(default="", validation_alias=AliasChoices("postgres_url", "url"))
    echo_sql: bool = False
    pool_size: int = 5
    pool_recycle: int = 300
    command_timeout: float | None = 30
    postgres_schema: str = ""

    @property
    def sqlite_path(self) -> str:
        return os.path.join(Path(self.sqlite_dir).resolve(), "app.db")

    @property
    def app_sqlalchemy_url(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        url = self.postgres_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def checkpointer_dsn(self) -> str:
        """libpq DSN for the checkpointer pool. The ``+asyncpg`` suffix is gone."""
        return dsn_with_search_path(normalize_libpq_dsn(self.postgres_url), self.postgres_schema)
