"""Build retrieval documents from loaded MCP tools."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_DOCUMENT_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    name: str
    display_name: str
    description: str
    tags: tuple[str, ...]
    parameters: tuple[str, ...]
    server: str
    content: str
    content_hash: str
    updated_at: int


def build_entry(
    tool: Any,
    *,
    tags: Sequence[str] = (),
    display_name: str | None = None,
    max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
    updated_at: int = 0,
) -> ToolCatalogEntry:
    name = str(getattr(tool, "name", "") or "").strip()
    description = str(getattr(tool, "description", "") or "").strip()
    metadata = getattr(tool, "metadata", None)
    if display_name is None and isinstance(metadata, dict):
        for key in ("display_name", "title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                display_name = value.strip()
                break
    display = (display_name or name).strip()
    parameters = _parameter_names(getattr(tool, "args_schema", None))
    cleaned_tags = tuple(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    content = _content(name, display, description, cleaned_tags, parameters, max_document_chars)
    return ToolCatalogEntry(
        name=name,
        display_name=display,
        description=description,
        tags=cleaned_tags,
        parameters=parameters,
        server="contextforge",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        updated_at=updated_at,
    )


def build_entries(
    tools: Sequence[Any],
    *,
    enrichment: Mapping[str, Mapping[str, Any]] | None = None,
    max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> list[ToolCatalogEntry]:
    entries: list[ToolCatalogEntry] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        extra = enrichment.get(name, {}) if enrichment else {}
        tags = extra.get("tags") if isinstance(extra, dict) else ()
        display = extra.get("display_name") if isinstance(extra, dict) else None
        entries.append(
            build_entry(
                tool,
                tags=tuple(tags or ()),
                display_name=display if isinstance(display, str) else None,
                max_document_chars=max_document_chars,
            )
        )
    return entries


def _parameter_names(schema: Any) -> tuple[str, ...]:
    if schema is None:
        return ()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return tuple(str(key) for key in properties)
        return ()
    fields = getattr(schema, "model_fields", None)
    if isinstance(fields, dict):
        return tuple(str(key) for key in fields)
    return ()


def _content(
    name: str,
    display_name: str,
    description: str,
    tags: Sequence[str],
    parameters: Sequence[str],
    max_chars: int,
) -> str:
    parts = [name, display_name]
    if description:
        parts.append(description)
    if tags:
        parts.append("tags: " + ", ".join(tags))
    if parameters:
        parts.append("parameters: " + ", ".join(parameters))
    text = "\n".join(parts)
    return text[: max(1, int(max_chars))]


__all__ = [
    "DEFAULT_MAX_DOCUMENT_CHARS",
    "ToolCatalogEntry",
    "build_entries",
    "build_entry",
]
