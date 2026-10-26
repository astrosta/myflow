"""Canonical, PostgreSQL-backed memory runtime.

The package is intentionally separate from the legacy LangGraph BaseStore
memory path.  New code should import from here; legacy modules stay readable
only for migration.
"""

from agentkit.agents.memory.canonical.operator_model import (
    operator_model_name,
    resolve_operator_model,
)

__all__ = ["operator_model_name", "resolve_operator_model"]
