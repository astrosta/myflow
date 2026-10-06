"""Envelope normalization and the db text encoding of content."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agentkit.runtime.events.catalog import RUN_EVENT_CATEGORIES, RunEventDefinition
from agentkit.runtime.events.constants import RUN_EVENT_ID_MAX_LENGTH
from agentkit.runtime.user_context import peek_current_user


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """唯一的写入入口：校验信封，并丢掉调用方传来的 seq / created_at（§5.6）。

    user_id 属于 store 的领域：没传就软读 ContextVar，读不到留 NULL 而不是失败。
    """
    for key in ("thread_id", "run_id", "event_type", "category"):
        if event.get(key) in (None, ""):
            raise ValueError(f"run event is missing {key}")
    thread_id, run_id = str(event["thread_id"]), str(event["run_id"])
    for name, value in (("thread_id", thread_id), ("run_id", run_id)):
        if len(value) > RUN_EVENT_ID_MAX_LENGTH:
            raise ValueError(f"{name} {value!r} exceeds {RUN_EVENT_ID_MAX_LENGTH} characters")
    definition = RunEventDefinition(str(event["event_type"]), str(event["category"]))
    if definition.category not in RUN_EVENT_CATEGORIES:
        raise ValueError(f"category {definition.category!r} is not in the catalog")
    content = event.get("content", "")
    if content is None:
        content = ""
    user_id = event["user_id"] if "user_id" in event else peek_current_user()
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "event_type": definition.event_type,
        "category": definition.category,
        "content": content,
        "metadata": dict(event.get("metadata") or {}),
        "user_id": user_id,
    }


def same_thread(events: list[dict[str, Any]]) -> str:
    thread_ids = {event["thread_id"] for event in events}
    if len(thread_ids) != 1:
        raise ValueError("put_batch events must share one thread_id")
    return events[0]["thread_id"]


def encode_for_db(
    content: Any,
    metadata: Mapping[str, Any],
    category: str,
    max_trace_content: int | None,
) -> tuple[str, dict[str, Any]]:
    """Dict content becomes text plus content_is_json. Only trace is truncated, by UTF-8 bytes."""
    stored = dict(metadata)
    if isinstance(content, dict):
        text = json.dumps(content, ensure_ascii=False)
        stored["content_is_json"] = True
    else:
        text = str(content)
    if category == "trace" and max_trace_content is not None:
        raw = text.encode("utf-8")
        if len(raw) > max_trace_content:
            text = raw[:max_trace_content].decode("utf-8", errors="ignore")
            stored["content_truncated"] = True
            stored.pop("content_is_json", None)
    return text, stored


def limit_trace(
    content: Any,
    metadata: Mapping[str, Any] | None,
    category: str,
    max_trace_content: int | None,
) -> tuple[Any, dict[str, Any]]:
    """Cut trace payloads by UTF-8 bytes. Message content is returned unchanged."""
    stored = dict(metadata or {})
    if category != "trace" or max_trace_content is None:
        return content, stored
    text = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else str(content)
    raw = text.encode("utf-8")
    if len(raw) <= max_trace_content:
        return content, stored
    stored["content_truncated"] = True
    return raw[:max_trace_content].decode("utf-8", errors="ignore"), stored


def decode_content(content: str, metadata: Mapping[str, Any], category: str) -> Any:
    """Restore dict content. Plain message text that is not JSON stays a string."""
    if metadata.get("content_is_json") or category == "message":
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content
