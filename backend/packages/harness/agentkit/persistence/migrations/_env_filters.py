"""Drop LangGraph's own tables out of autogenerate."""

from __future__ import annotations

LANGGRAPH_OWNED_TABLES = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
)


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Keep checkpoint tables and the indexes hanging off them out of our chain."""
    del reflected, compare_to
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    parent = getattr(object_, "table", None)
    parent_name = getattr(parent, "name", None) if parent is not None else None
    if parent_name in LANGGRAPH_OWNED_TABLES:
        return False
    return True
