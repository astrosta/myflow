"""Metadata tags for tools loaded from ContextForge."""

from __future__ import annotations

from typing import Any

CONTEXTFORGE_TOOL_META_KEY = "myflow_contextforge"


def tag_contextforge_tool(tool: Any, *, original_name: str) -> Any:
    tool.metadata = {
        **(getattr(tool, "metadata", None) or {}),
        CONTEXTFORGE_TOOL_META_KEY: {"original_name": original_name},
    }
    return tool


def is_contextforge_tool(tool: Any) -> bool:
    metadata = getattr(tool, "metadata", None)
    return isinstance(metadata, dict) and CONTEXTFORGE_TOOL_META_KEY in metadata


def contextforge_original_name(tool: Any) -> str | None:
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(CONTEXTFORGE_TOOL_META_KEY)
    if not isinstance(entry, dict):
        return None
    name = entry.get("original_name")
    return name if isinstance(name, str) and name else None


__all__ = [
    "CONTEXTFORGE_TOOL_META_KEY",
    "contextforge_original_name",
    "is_contextforge_tool",
    "tag_contextforge_tool",
]
