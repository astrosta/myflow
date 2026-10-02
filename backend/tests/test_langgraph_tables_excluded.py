"""§6.1 LangGraph checkpoint tables stay out of our autogenerate."""

from sqlalchemy import Column, Index, Integer, MetaData, Table

from agentkit.persistence.migrations._env_filters import LANGGRAPH_OWNED_TABLES, include_object


def test_checkpoint_tables_and_their_indexes_are_excluded() -> None:
    assert LANGGRAPH_OWNED_TABLES == {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
    metadata = MetaData()
    checkpoints = Table("checkpoints", metadata, Column("id", Integer))
    index = Index("ix_checkpoints_id", checkpoints.c.id)
    assert include_object(checkpoints, "checkpoints", "table", True, None) is False
    assert include_object(index, index.name, "index", True, None) is False
    runs = Table("runs", metadata, Column("id", Integer))
    assert include_object(runs, "runs", "table", True, None) is True
