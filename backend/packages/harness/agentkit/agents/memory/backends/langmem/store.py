"""LangGraph BaseStore construction for memory (M10 section 2.5.3)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agentkit.config.database_config import DatabaseConfig, dsn_with_search_path, normalize_libpq_dsn

logger = logging.getLogger(__name__)


def _index_config(backend_config: dict[str, Any]) -> dict[str, Any] | None:
    if str(backend_config.get("retrieval_adapter", "lexical")).lower() != "vector":
        return None
    model = backend_config.get("embedding_model")
    dims = backend_config.get("embedding_dimensions")
    if not model or not dims:
        logger.warning(
            "memory.backend_config.retrieval_adapter=vector requires embedding_model and "
            "embedding_dimensions; falling back to lexical retrieval"
        )
        return None
    try:
        from langchain.embeddings import init_embeddings

        embed = init_embeddings(str(model))
    except Exception:
        logger.warning("Memory embedding model %r could not be built; using lexical fallback", model, exc_info=True)
        return None
    return {
        "dims": int(dims),
        "embed": embed,
        "fields": list(backend_config.get("index_fields") or ["search_text"]),
    }


@contextmanager
def make_memory_store(
    database: DatabaseConfig, backend_config: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Open the persistent store selected by database.backend.

    memory: InMemoryStore (tests / local temporary)
    sqlite: SqliteStore on database.sqlite_dir/app.db
    postgres: PostgresStore on the same DSN as the checkpointer
    """
    index = _index_config(dict(backend_config or {}))
    if database.backend == "memory":
        from langgraph.store.memory import InMemoryStore

        logger.info("Memory store: InMemoryStore (in-process, not persistent)")
        yield InMemoryStore(index=index)
        return

    if database.backend == "sqlite":
        from langgraph.store.sqlite import SqliteStore

        path = Path(database.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteStore.from_conn_string(str(path), index=index) as store:
            store.setup()
            logger.info("Memory store: SqliteStore at %s", path)
            yield store
        return

    if database.backend == "postgres":
        from langgraph.store.postgres import PostgresStore

        dsn = dsn_with_search_path(
            normalize_libpq_dsn(database.postgres_url), database.postgres_schema
        )
        if not dsn:
            raise ValueError("database.backend is postgres but memory store has no DSN")
        with PostgresStore.from_conn_string(dsn, index=index) as store:
            store.setup()
            logger.info("Memory store: PostgresStore")
            yield store
        return

    raise ValueError(f"unknown database backend: {database.backend!r}")
