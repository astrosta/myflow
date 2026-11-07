"""Metadata tags for tools loaded from ContextForge."""

from __future__ import annotations

from typing import Any

CONTEXTFORGE_TOOL_META_KEY = "myflow_contextforge"


def tag_contextforge_tool(
    tool: Any,
    *,
    original_name: str,
    owner: str | None = None,
) -> Any:
    metadata = getattr(tool, "metadata", None)
    entry = dict(metadata.get(CONTEXTFORGE_TOOL_META_KEY) or {}) if isinstance(metadata, dict) else {}
    entry["original_name"] = original_name
    if owner:
        entry["owner"] = owner
    else:
        entry.pop("owner", None)
    tool.metadata = {
        **(metadata or {}),
        CONTEXTFORGE_TOOL_META_KEY: entry,
    }
    return tool


def is_contextforge_tool(tool: Any) -> bool:
    metadata = getattr(tool, "metadata", None)
    return isinstance(metadata, dict) and CONTEXTFORGE_TOOL_META_KEY in metadata


def contextforge_owner(tool: Any) -> str | None:
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(CONTEXTFORGE_TOOL_META_KEY)
    if not isinstance(entry, dict):
        return None
    owner = entry.get("owner")
    return owner if isinstance(owner, str) and owner else None


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
    "contextforge_owner",
    "is_contextforge_tool",
    "tag_contextforge_tool",
]
