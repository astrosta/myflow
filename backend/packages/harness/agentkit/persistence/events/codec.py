"""Envelope normalization and the db text encoding of content."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from agentkit.runtime.events.catalog import RUN_EVENT_CATEGORIES, RunEventDefinition
from agentkit.runtime.events.constants import RUN_EVENT_ID_MAX_LENGTH
from agentkit.runtime.user_context import peek_current_user

logger = logging.getLogger(__name__)

#: 每个"不认识的类型"只提示一次。写事件是热路径，按条打日志会把日志刷爆。
_warned_non_json_types: set[str] = set()


def _type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _warn_non_json_once(value: Any, *, how: str) -> None:
    name = _type_name(value)
    if name in _warned_non_json_types:
        return
    _warned_non_json_types.add(name)
    logger.warning(
        "run event content holds %s, which json cannot encode; stored as %s (warned once per type)",
        name,
        how,
    )


def json_default(value: Any) -> Any:
    """`json.dumps` 的 `default=`：让 LangChain 消息这类对象也能被写下去。

    **一次 `put_batch` 里混进一条 `SystemMessage`，不该让整批事件丢失**——
    那是"消息发出去、列表里什么都没有"这类最难查的故障（写入端没有异常、
    读取端也没有异常，只是少了数据）。

    三档处理，按"信息丢得最少"排序：

    1. **Pydantic v2 对象**（LangChain 的 `BaseMessage` 就是）→ `model_dump()`。
       结构完整地落下去，读回来还是 `{"type": "system", "content": ..., ...}`。
    2. 有 `content` 属性但不是 pydantic（自定义消息类）→ 只留 `content`。
    3. 其它 → `str(value)`，并且**每个类型 warning 一次**。

    任何一步失败都退到下一档：`default` 里抛异常 = 整批写入失败，比丢结构更糟。
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 —— 退到下一档，绝不把整批写入带崩
            logger.warning("model_dump() failed for %s; falling back", _type_name(value), exc_info=True)

    content = getattr(value, "content", None)
    if content is not None and not callable(content):
        _warn_non_json_once(value, how="its content")
        return content

    _warn_non_json_once(value, how="str(value)")
    return str(value)


def dumps(value: Any) -> str:
    """**事件目录里唯一允许直接用的 JSON 编码器。**

    三个调用点（`encode_for_db` / `limit_trace` / jsonl 落盘）共用它，
    免得其中一处忘了带 `default=` —— 那一处就是下一个"整批失败"。
    """
    return json.dumps(value, ensure_ascii=False, default=json_default)


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
    """Non-string content becomes JSON text plus `content_is_json`.

    Only trace is truncated, by UTF-8 bytes.

    `str` 之外的一切都走 JSON（`default=json_default`）：dict、list、
    **LangChain 消息对象**都是。以前只有 `dict` 走 JSON、其余 `str()`，
    于是 `SystemMessage` 会被写成 `content='...' additional_kwargs={}` 这种
    `repr`——不是不能存，是读回来再也变不回结构。
    """
    stored = dict(metadata)
    if isinstance(content, str):
        text = content
    else:
        try:
            text = dumps(content)
            stored["content_is_json"] = True
        except (TypeError, ValueError):
            # `default` 已经兜了一层，走到这儿说明连 `str()` 都要试：
            # 宁可退化成字符串，也不要让这次写事件失败。
            logger.warning(
                "content of type %s is not JSON-encodable; storing str()", _type_name(content)
            )
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
    text = content if isinstance(content, str) else dumps(content)
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
