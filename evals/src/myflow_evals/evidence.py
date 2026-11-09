"""Pure normalization of a finished run into evaluation evidence.

This module is a parser, not a client: no I/O, no clock, no prints. It takes the
checkpoint's message list and the persisted run-event ledger and returns one
``RunEvidence``. Every field is best-effort. A row that does not have the shape
the producer documented is dropped, never raised: "could not read" must not turn
into a failed scored run.

The ledger shapes mirrored here were verified against commit b251d0b:
``llm.ai.response`` and ``llm.tool.result`` carry ``AIMessage.model_dump()`` /
``ToolMessage.model_dump()``, ``subagent.start`` joins a task call by
``metadata["task_id"]`` (the provider tool_call_id), and ``retrieval.tool``,
``retrieval.skill``, ``retrieval.kb`` carry the final model-visible retrieval
result. The parser does not re-validate the producer's decisions; it only joins
and flattens them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Design-doc line 799: these are infrastructure, not business, tool calls.
_INFRASTRUCTURE_TOOLS = frozenset(
    {"tool_search", "tool_execute", "task", "search_skills", "load_skill", "knowledge_search"}
)

_AI_MESSAGE_TYPE = "ai"
_TOOL_MESSAGE_TYPE = "tool"
_AI_RESPONSE_EVENT = "llm.ai.response"
_TOOL_RESULT_EVENT = "llm.tool.result"
_SUBAGENT_START_EVENT = "subagent.start"
_RETRIEVAL_TOOL_EVENT = "retrieval.tool"
_RETRIEVAL_SKILL_EVENT = "retrieval.skill"
_RETRIEVAL_KB_EVENT = "retrieval.kb"
_CITATIONS_KEY = "knowledge_citations"


@dataclass
class RunEvidence:
    """Everything the rule evaluators need from one finished run."""

    answer: str
    tool_calls: list[dict[str, Any]]
    business_tool_calls: list[dict[str, Any]]
    subagent_calls: list[dict[str, Any]]
    retrieved_tools: list[str]
    retrieved_skills: list[str]
    loaded_skills: list[str]
    kb_called: bool
    retrieved_chunks: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    trace_id: str | None


def collect_evidence(
    messages: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    trace_id: str | None = None,
) -> RunEvidence:
    """Normalize one run's checkpoint messages and event-ledger rows.

    ``messages`` is the checkpoint's message list (``values.messages``) and
    ``events`` comes from ``GET .../runs/{run_id}/events``. Events that name
    another run are ignored so mixed input cannot contaminate one sample.
    ``trace_id`` is the runner's handoff from ``RunSnapshot.trace_id``, which
    ``wait_run`` parsed from ``metadata["myflow-trace_id"]``; it stays ``None``
    when no run snapshot was read.
    """
    event_rows = _event_rows(events, run_id)
    ai_rows = _dedupe_messages(
        _message_rows(messages, _AI_MESSAGE_TYPE)
        + _event_contents(event_rows, _AI_RESPONSE_EVENT)
    )
    tool_results = _tool_results(messages, event_rows)
    calls = _tool_calls(ai_rows, tool_results)
    final_ai = _final_ai(messages, event_rows)
    return RunEvidence(
        answer=_content_text(final_ai.get("content")) if final_ai is not None else "",
        tool_calls=calls,
        business_tool_calls=_business_tool_calls(calls),
        subagent_calls=_subagent_calls(calls, event_rows),
        retrieved_tools=_retrieved_names(
            event_rows, _RETRIEVAL_TOOL_EVENT, "tools", "call_name"
        ),
        retrieved_skills=_retrieved_names(
            event_rows, _RETRIEVAL_SKILL_EVENT, "skills", "name"
        ),
        loaded_skills=_loaded_skills(calls),
        kb_called=any(
            event.get("event_type") == _RETRIEVAL_KB_EVENT for event in event_rows
        ),
        retrieved_chunks=_retrieved_chunks(event_rows),
        citations=_citations(_last_ai(messages, event_rows)),
        trace_id=trace_id,
    )


def _event_rows(
    events: Sequence[Mapping[str, Any]], run_id: str
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_run_id = event.get("run_id")
        if isinstance(event_run_id, str) and event_run_id and event_run_id != run_id:
            continue
        rows.append(event)
    return rows


def _message_rows(
    messages: Sequence[Mapping[str, Any]], message_type: str
) -> list[Mapping[str, Any]]:
    return [
        message
        for message in messages
        if isinstance(message, Mapping) and message.get("type") == message_type
    ]


def _event_contents(
    events: Sequence[Mapping[str, Any]], event_type: str
) -> list[Mapping[str, Any]]:
    contents: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("event_type") != event_type:
            continue
        content = event.get("content")
        if isinstance(content, Mapping):
            contents.append(content)
    return contents


def _dedupe_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Checkpoint and ledger copies of the same message share its id."""
    seen: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for message in messages:
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in seen:
                continue
            seen.add(message_id)
        unique.append(message)
    return unique


def _tool_results(
    messages: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """Map tool_call_id to result text; checkpoint rows win over ledger copies."""
    results: dict[str, str] = {}
    rows = _message_rows(messages, _TOOL_MESSAGE_TYPE) + _event_contents(
        events, _TOOL_RESULT_EVENT
    )
    for row in rows:
        call_id = row.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id or call_id in results:
            continue
        results[call_id] = _content_text(row.get("content"))
    return results


def _tool_calls(
    ai_rows: Sequence[Mapping[str, Any]], results: Mapping[str, str]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ai_rows:
        raw_calls = row.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw in raw_calls:
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            call_id = _tool_call_id(raw)
            if call_id is not None:
                if call_id in seen:
                    continue
                seen.add(call_id)
            calls.append(
                {
                    "tool_call_id": call_id,
                    "name": name,
                    "args": _args_mapping(raw.get("args")),
                    "result": results.get(call_id, "") if call_id is not None else "",
                }
            )
    return calls


def _tool_call_id(raw: Mapping[str, Any]) -> str | None:
    for key in ("id", "tool_call_id"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _business_tool_calls(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Unwrap tool_execute and drop infrastructure calls; keep the inner tool name."""
    business: list[dict[str, Any]] = []
    for call in calls:
        name = call["name"]
        args = call["args"]
        if name == "tool_execute":
            inner_name = args.get("tool_name")
            if not isinstance(inner_name, str) or not inner_name:
                continue
            if inner_name in _INFRASTRUCTURE_TOOLS:
                continue
            business.append(
                {
                    "tool_call_id": call["tool_call_id"],
                    "name": inner_name,
                    "args": _args_mapping(args.get("arguments")),
                    "result": call["result"],
                }
            )
            continue
        if name in _INFRASTRUCTURE_TOOLS:
            continue
        business.append(dict(call))
    return business


def _loaded_skills(calls: Sequence[Mapping[str, Any]]) -> list[str]:
    loaded: list[str] = []
    for call in calls:
        if call["name"] != "load_skill":
            continue
        name = call["args"].get("name")
        if isinstance(name, str) and name:
            loaded.append(name)
    return loaded


def _subagent_calls(
    calls: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Join each task tool call (routing) with its subagent.start row (actual start)."""
    starts: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("event_type") != _SUBAGENT_START_EVENT:
            continue
        metadata = event.get("metadata")
        task_id = metadata.get("task_id") if isinstance(metadata, Mapping) else None
        if not isinstance(task_id, str) or not task_id or task_id in starts:
            continue
        starts[task_id] = event

    subagents: list[dict[str, Any]] = []
    matched: set[str] = set()
    for call in calls:
        if call["name"] != "task":
            continue
        call_id = call["tool_call_id"]
        start = starts.get(call_id) if isinstance(call_id, str) else None
        subagent_type = call["args"].get("subagent_type")
        entry = {
            "tool_call_id": call_id,
            "subagent_type": (
                subagent_type if isinstance(subagent_type, str) and subagent_type else None
            ),
            "args": call["args"],
            "started": start is not None,
            "model_name": None,
            "description": None,
        }
        if start is not None:
            matched.add(call_id)
            _copy_start_details(entry, start, keep_subagent_type=True)
        subagents.append(entry)

    for task_id, start in starts.items():
        if task_id in matched:
            continue
        entry = {
            "tool_call_id": task_id,
            "subagent_type": None,
            "args": {},
            "started": True,
            "model_name": None,
            "description": None,
        }
        _copy_start_details(entry, start, keep_subagent_type=False)
        subagents.append(entry)
    return subagents


def _copy_start_details(
    entry: dict[str, Any], start: Mapping[str, Any], *, keep_subagent_type: bool
) -> None:
    sources = [start.get("content"), start.get("metadata")]
    if not keep_subagent_type:
        subagent_type = _first_string(sources, "subagent_type")
        if subagent_type is not None:
            entry["subagent_type"] = subagent_type
    entry["model_name"] = _first_string(sources, "model_name")
    entry["description"] = _first_string(sources, "description")


def _first_string(sources: Sequence[Any], key: str) -> str | None:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _retrieved_names(
    events: Sequence[Mapping[str, Any]],
    event_type: str,
    list_key: str,
    name_key: str,
) -> list[str]:
    names: list[str] = []
    for event in events:
        if event.get("event_type") != event_type:
            continue
        content = event.get("content")
        if not isinstance(content, Mapping):
            continue
        items = content.get(list_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            value = item.get(name_key)
            if isinstance(value, str) and value:
                names.append(value)
    return names


def _retrieved_chunks(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != _RETRIEVAL_KB_EVENT:
            continue
        content = event.get("content")
        if not isinstance(content, Mapping):
            continue
        items = content.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping):
                chunks.append(dict(item))
    return chunks


def _last_ai(
    messages: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """Citations belong to the last AI row even when it produced no answer text."""
    message_rows = _message_rows(messages, _AI_MESSAGE_TYPE)
    if message_rows:
        return message_rows[-1]
    event_ai_rows = _event_contents(events, _AI_RESPONSE_EVENT)
    if event_ai_rows:
        return event_ai_rows[-1]
    return None


def _final_ai(
    messages: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """Last AI message with non-empty text, preferring the checkpoint messages."""
    for row in reversed(_message_rows(messages, _AI_MESSAGE_TYPE)):
        if _content_text(row.get("content")).strip():
            return row
    for row in reversed(_event_contents(events, _AI_RESPONSE_EVENT)):
        if _content_text(row.get("content")).strip():
            return row
    return None


def _citations(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if row is None:
        return []
    additional_kwargs = row.get("additional_kwargs")
    if not isinstance(additional_kwargs, Mapping):
        return []
    raw = additional_kwargs.get(_CITATIONS_KEY)
    if not isinstance(raw, list):
        return []
    citations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if not all(
            isinstance(item.get(key), str) and item.get(key)
            for key in ("citation_id", "chunk_id", "document_id")
        ):
            continue
        citations.append(dict(item))
    return citations


def _args_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _content_text(content: Any) -> str:
    """Mirror the backend's message text extractor without importing agentkit."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""
