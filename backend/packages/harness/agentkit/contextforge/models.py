"""Canonical ContextForge tool models and safe error types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.contextforge.ownership import owner_from_annotations


class ContextForgeError(RuntimeError):
    """Base error for ContextForge integration failures."""

    code = "contextforge_error"
    status_code = 502


class ContextForgeUnavailable(ContextForgeError):
    code = "contextforge_unavailable"
    status_code = 502


class ContextForgeTimeout(ContextForgeError):
    code = "contextforge_timeout"
    status_code = 504


class ContextForgeAuthError(ContextForgeError):
    code = "contextforge_auth_failed"
    status_code = 502


class ContextForgeNotFound(ContextForgeError):
    code = "tool_not_found"
    status_code = 404


class ContextForgeConflict(ContextForgeError):
    code = "tool_conflict"
    status_code = 409


class ContextForgeValidationError(ContextForgeError):
    code = "contextforge_validation_error"
    status_code = 422


class ContextForgeProtocolError(ContextForgeError):
    code = "contextforge_protocol_error"
    status_code = 502


@dataclass(frozen=True, slots=True)
class ContextForgeTool:
    """One ContextForge tool, normalized to MyFlow's wire vocabulary."""

    id: str
    name: str
    display_name: str | None = None
    description: str = ""
    url: str | None = None
    enabled: bool = True
    reachable: bool = True
    integration_type: str = "REST"
    request_type: str = "SSE"
    headers: dict[str, str] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    owner: str | None = None
    tags: tuple[str, ...] = ()
    visibility: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextForgeAdminEvent:
    type: str
    data: dict[str, Any]
    raw: str = ""


class ToolWrite(BaseModel):
    """Admin write shape. enabled is handled by the state endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str | None = Field(default=None, max_length=255)
    description: str = Field(default="", max_length=100_000)
    integration_type: Literal["REST", "MCP", "A2A"] = "REST"
    request_type: str = "SSE"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    visibility: Literal["private", "team", "public"] | None = None
    enabled: bool = True

    def to_create_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "integration_type": self.integration_type,
            "request_type": self.request_type,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
            "tags": list(self.tags),
        }
        if self.display_name:
            payload["displayName"] = self.display_name
        if self.url:
            payload["url"] = self.url
        if self.headers:
            payload["headers"] = dict(self.headers)
        if self.visibility:
            payload["visibility"] = self.visibility
        return payload

    def to_update_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "integration_type": self.integration_type,
            "input_schema": self.input_schema,
            "annotations": self.annotations,
            "tags": list(self.tags),
        }
        # ContextForge ToolUpdate only accepts HTTP methods for request_type.
        if self.request_type in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
            payload["request_type"] = self.request_type
        if self.display_name:
            payload["displayName"] = self.display_name
        if self.url:
            payload["url"] = self.url
        if self.headers:
            payload["headers"] = dict(self.headers)
        if self.visibility:
            payload["visibility"] = self.visibility
        return payload


def normalize_input_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ContextForgeProtocolError("tool input_schema is not valid JSON") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ContextForgeProtocolError("tool input_schema must be an object")


def normalize_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        headers: dict[str, str] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("name", item.get("key"))
            val = item.get("value")
            if isinstance(key, str) and val is not None:
                headers[key] = str(val)
        return headers
    raise ContextForgeProtocolError("tool headers must be an object or list")


def normalize_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    tags: list[str] = []
    for item in value:
        if isinstance(item, str):
            tag = item
        elif isinstance(item, dict):
            candidate = item.get("name") or item.get("label") or item.get("value")
            tag = str(candidate) if candidate else ""
        else:
            tag = ""
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags)


def tool_from_wire(data: dict[str, Any]) -> ContextForgeTool:
    if not isinstance(data, dict):
        raise ContextForgeProtocolError("tool response is not an object")
    tool_id = data.get("id")
    name = data.get("name")
    if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
        raise ContextForgeProtocolError("tool response is missing id or name")
    annotations = data.get("annotations") if isinstance(data.get("annotations"), dict) else {}
    return ContextForgeTool(
        id=tool_id,
        name=name,
        display_name=_first_text(data, "displayName", "display_name", "title"),
        description=str(data.get("description") or ""),
        url=_text_or_none(data.get("url")),
        enabled=bool(data.get("enabled", True)),
        reachable=bool(data.get("reachable", True)),
        integration_type=str(data.get("integration_type") or data.get("integrationType") or "REST"),
        request_type=str(data.get("request_type") or data.get("requestType") or "SSE"),
        headers=normalize_headers(data.get("headers")),
        input_schema=normalize_input_schema(data.get("input_schema", data.get("inputSchema"))),
        annotations=annotations,
        owner=owner_from_annotations(annotations),
        tags=normalize_tags(data.get("tags")),
        visibility=_text_or_none(data.get("visibility")),
        created_at=_text_or_none(data.get("created_at") or data.get("createdAt")),
        updated_at=_text_or_none(data.get("updated_at") or data.get("updatedAt")),
        raw=dict(data),
    )


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "ContextForgeAdminEvent",
    "ContextForgeAuthError",
    "ContextForgeConflict",
    "ContextForgeError",
    "ContextForgeNotFound",
    "ContextForgeProtocolError",
    "ContextForgeTimeout",
    "ContextForgeTool",
    "ContextForgeUnavailable",
    "ContextForgeValidationError",
    "ToolWrite",
    "normalize_headers",
    "normalize_input_schema",
    "normalize_tags",
    "tool_from_wire",
]
