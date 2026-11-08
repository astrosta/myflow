"""Retrieval text for one MCP tool, built from what `tools/list` advertises.

The parameter extraction and the document layout follow the tool-catalog builder
that the retired external gateway used, so the BM25 channel of `myflow_mcp_tools`
keeps scoring the same way.  Two things differ, and both come from MCP owning the
catalog now:

* the input schema arrives as JSON (`Tool.inputSchema`), not as a Pydantic model, so
  `parameter_names` reads `properties` - and nothing else, because there is nothing
  else to read;
* the server slug is part of the text.  Two users' servers may both advertise
  `search`, and a query that names one of them has to be able to reach it.

The full `inputSchema` is still deliberately absent from the document: it is stored
verbatim in `mcp_tools` and never embedded, so a schema-only edit leaves the vector
index alone and only the schema hash moves.

Nothing here touches the network or the database: one tool in, one document out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The embedding/BM25 budget of one tool document.  A pathological upstream
#: description is truncated instead of being allowed to set the cost of every sync.
DEFAULT_MAX_DOCUMENT_CHARS = 4000


@dataclass(frozen=True, slots=True)
class McpToolDocument:
    """The retrieval-facing projection of one MCP tool.

    `display_name` is the upstream `title` (or `None`, so the UI falls back to the
    tool name), `parameters` are the declared input property names in declared order
    (they feed the search card), and `content` is the text that is embedded.
    """

    display_name: str | None
    parameters: tuple[str, ...]
    content: str


def parameter_names(schema: Any) -> tuple[str, ...]:
    """The declared parameter names of an input schema, in declared order.

    Never raises: an unreadable or absent schema simply contributes no parameter
    names, because this feeds the search text and a card, not the call itself.
    """
    if not isinstance(schema, Mapping):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    return tuple(str(key) for key in properties)


def build_tool_content(
    name: str,
    *,
    display_name: str = "",
    server: str = "",
    description: str = "",
    parameters: Sequence[str] = (),
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> str:
    """The BM25/embedding text for one tool: names, server, description, parameters.

    The full input schema is deliberately absent: it is stored verbatim in the
    database and never embedded, so a schema-only edit leaves the vector alone.
    A display name that merely repeats the tool name is written once.
    """
    parts: list[str] = []
    tool_name = str(name or "").strip()
    display = str(display_name or "").strip()
    server_name = str(server or "").strip()
    if tool_name:
        parts.append(tool_name)
    if display and display != tool_name:
        parts.append(display)
    if server_name:
        parts.append("server: " + server_name)
    text = str(description or "").strip()
    if text:
        parts.append(text)
    names = [
        str(parameter).strip()
        for parameter in parameters
        if str(parameter).strip()
    ]
    if names:
        parts.append("parameters: " + ", ".join(names))
    return "\n".join(parts)[: max(1, int(max_chars))]


def build_tool_document(
    *,
    name: str,
    title: str | None = None,
    description: str | None = None,
    server: str = "",
    input_schema: Mapping[str, Any] | None = None,
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> McpToolDocument:
    """Build the document for one `mcp.types.Tool` of one server."""
    display = str(title or "").strip() or None
    parameters = parameter_names(input_schema)
    content = build_tool_content(
        name,
        display_name=display or "",
        server=server,
        description=str(description or ""),
        parameters=parameters,
        max_chars=max_chars,
    )
    return McpToolDocument(
        display_name=display,
        parameters=parameters,
        content=content,
    )


__all__ = [
    "DEFAULT_MAX_DOCUMENT_CHARS",
    "McpToolDocument",
    "build_tool_content",
    "build_tool_document",
    "parameter_names",
]
