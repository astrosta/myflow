"""Process-local ledger. Seq is monotonic only inside this process."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from agentkit.persistence.base import iso_utc
from agentkit.persistence.events.base import RunEventStore
from agentkit.persistence.events.codec import dumps, normalize_event, same_thread


class MemoryRunEventStore(RunEventStore):
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()
        return lock

    async def put(self, event: dict[str, Any]) -> None:
        await self.put_batch([event])

    async def put_batch(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        if not events:
            return []
        normalized = [normalize_event(event) for event in events]
        thread_id = same_thread(normalized)
        async with self._lock_for(thread_id):
            return self._append(thread_id, normalized)

    async def put_if_absent(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = normalize_event(event)
        async with self._lock_for(normalized["thread_id"]):
            existing = self._find(normalized)
            if existing is not None:
                return copy.deepcopy(existing), False
            return self._append(normalized["thread_id"], [normalized])[0], True

    async def list_messages(
        self,
        thread_id: str,
        limit: int,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [row for row in self._events.get(thread_id, []) if row["category"] == "message"]
        return [_copy(row) for row in _page(rows, limit, before_seq, after_seq)]

    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        event_types: Sequence[str] | None = None,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._events.get(thread_id, [])
            if row["run_id"] == run_id and _event_visible(row, event_types, after_seq)
        ]
        rows.sort(key=lambda row: row["seq"])
        return [_copy(row) for row in rows]

    async def count_messages(self, thread_id: str) -> int:
        return sum(1 for row in self._events.get(thread_id, []) if row["category"] == "message")

    def _append(self, thread_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bucket = self._events.setdefault(thread_id, [])
        last = bucket[-1]["seq"] if bucket else 0
        created_at = iso_utc(datetime.now(UTC))
        stored = []
        for index, event in enumerate(events):
            record = _public(event, seq=last + index + 1, created_at=created_at)
            stored.append(record)
        bucket.extend(stored)
        return [_copy(row) for row in stored]

    def _find(self, event: dict[str, Any]) -> dict[str, Any] | None:
        for row in self._events.get(event["thread_id"], []):
            if row["run_id"] == event["run_id"] and row["event_type"] == event["event_type"]:
                return row
        return None


def _public(
    event: dict[str, Any],
    *,
    seq: int,
    created_at: str | None,
    json_safe: bool = False,
) -> dict[str, Any]:
    """行形状的唯一出处。`json_safe=True` 时把不是 JSON 原生值的 content 归一。

    默认 **False**：内存后端"放进去什么、拿出来什么"是它存在的意义（调试、测试、
    以及 run_end 那种"整份 outputs"的载荷）。会**落盘**的后端（jsonl）传 True，
    这样它 `put_batch` 返回的那一行和之后从盘上读回来的是同一个形状。
    """
    content = _json_safe(event["content"]) if json_safe else event["content"]
    record = {
        "thread_id": event["thread_id"],
        "run_id": event["run_id"],
        "event_type": event["event_type"],
        "category": event["category"],
        "content": copy.deepcopy(content) if isinstance(content, dict) else content,
        "metadata": dict(event["metadata"]),
        "seq": seq,
        "created_at": created_at,
    }
    if event.get("user_id") is not None:
        record["user_id"] = event["user_id"]
    return record


def _is_json_native(value: Any) -> bool:
    """递归判断：这个值能不能原样进 JSON（`str` 键的 dict / list / 标量）。

    先判再转，是为了让**正常**的 payload 一个字节都不动（`json` 往返会把
    int 键变成字符串、tuple 变成 list）；只有真的混进了非原生值才走那一趟。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_native(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_is_json_native(item) for item in value)
    return False


def _json_safe(content: Any) -> Any:
    """把"不是 JSON 原生值"的 content 收敛掉（`SystemMessage` 就是这一类）。

    不这么做的话，同一条事件有两个形状：读盘（或读库）是一个 dict，
    读内存（jsonl 的 `put_batch` 返回的也是内存那一份）是一个 **Python 对象**。
    对象一旦漏到 API / SSE 层，表现是序列化失败，而不是"这条消息少了个字段"。
    """
    if _is_json_native(content):
        return content
    try:
        return json.loads(dumps(content))
    except (TypeError, ValueError):
        return str(content)


def _copy(row: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(row)


def _event_visible(row: dict[str, Any], event_types: Sequence[str] | None, after_seq: int | None) -> bool:
    if event_types is not None and row["event_type"] not in event_types:
        return False
    if after_seq is not None and row["seq"] <= after_seq:
        return False
    return True


def _page(
    rows: list[dict[str, Any]],
    limit: int,
    before_seq: int | None,
    after_seq: int | None,
) -> list[dict[str, Any]]:
    if before_seq is not None:
        rows = [row for row in rows if row["seq"] < before_seq]
    if after_seq is not None:
        rows = [row for row in rows if row["seq"] > after_seq]
    if before_seq is not None:
        chosen = sorted(rows, key=lambda row: row["seq"], reverse=True)[:limit]
        chosen.reverse()
        return chosen
    if after_seq is not None:
        return sorted(rows, key=lambda row: row["seq"])[:limit]
    chosen = sorted(rows, key=lambda row: row["seq"], reverse=True)[:limit]
    chosen.reverse()
    return chosen
