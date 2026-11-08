"""The MCP runtime contract ``tool_execute`` executes against.

``tool_execute`` needs exactly four things from the process: the tool catalog, the
server configuration behind it, a way to re-sync one server, and a way to run one
tool on the server that owns it.  None of those needs the concrete runtime class,
so this module declares a Protocol and publishes one process-global slot for
whatever the application built at startup:

* Task 8's ``McpToolRuntime`` calls ``set_mcp_runtime(runtime)`` when it starts and
  ``reset_mcp_runtime()`` when it stops;
* ``tool_execute`` reads the slot through ``get_mcp_runtime()`` on every call, so a
  runtime published (or withdrawn) later is picked up without re-importing the
  tool, and a test can publish a fake without building the real runtime;
* an unpublished slot means "this deployment has no MCP execution", which
  ``tool_execute`` reports as ``mcp_unavailable`` instead of guessing.

Four optional members are read defensively by the tool instead of being declared
here, because a fake runtime should not have to implement them:
``require_retrieved`` (the retrieval gate, falling back to
``config.tool_search.require_retrieved``), ``retry_on_tool_error`` (the
refresh-and-retry switch, default on), ``record_retry(tool_name=`, `result=)``
(the sync bookkeeping the retry reports into), and ``config`` (read for the
retrieval policy).  The runtime must keep all four: the first two decide whether
the fallback runs at all, and the third is how a recovered call is counted.

``call_tool`` returns the MCP ``CallToolResult`` even when ``isError`` is true: that
flag is the tool's own answer, data for the executor to report, not a transport
failure.  Only an exception means the call did not produce a result.
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp import types as mcp_types

from agentkit.mcp.store.repository import McpServerStore
from agentkit.mcp.store.tool_repository import McpToolRecord, McpToolStore


class McpRuntimeProtocol(Protocol):
    """What the executor needs from the process-wide MCP runtime."""

    tool_store: McpToolStore
    server_store: McpServerStore

    async def refresh_server(self, server_id: str) -> Any:
        """Re-discover one server's tools and persist the new snapshot.

        Must return the outcome *for that server* - an object exposing ``error``
        (the failure this sync hit, or None) and ``index_failed`` (the database
        snapshot was committed and only the derived index failed).  A global
        catalog summary is not enough: the executor uses it to decide whether the
        re-read that follows saw a fresh snapshot, and a whole-catalog failure
        would otherwise veto (or a whole-catalog success would misreport) the
        retry of one target.  Raises are caught and treated as "did not refresh";
        ``None`` means "no failure reported" and is accepted.
        """
        ...

    async def call_tool(
        self,
        record: McpToolRecord,
        arguments: dict[str, Any],
    ) -> mcp_types.CallToolResult:
        """Run one tool on the server that owns it, by the record's remote name."""
        ...


_runtime: McpRuntimeProtocol | None = None


def set_mcp_runtime(runtime: McpRuntimeProtocol | None) -> None:
    """Publish the process runtime; ``None`` withdraws it."""
    global _runtime
    _runtime = runtime


def get_mcp_runtime() -> McpRuntimeProtocol | None:
    """The published runtime, or ``None`` when this process has none."""
    return _runtime


def reset_mcp_runtime() -> None:
    """Forget the published runtime (shutdown, and every test's teardown)."""
    global _runtime
    _runtime = None


__all__ = [
    "McpRuntimeProtocol",
    "get_mcp_runtime",
    "reset_mcp_runtime",
    "set_mcp_runtime",
]
