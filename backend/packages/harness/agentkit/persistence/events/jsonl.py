"""Single-process jsonl ledger. Seq is recovered by scanning the thread directory after restart."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentkit.persistence.base import iso_utc
from agentkit.persistence.events.base import RunEventStore
from agentkit.persistence.events.codec import dumps, normalize_event, same_thread
from agentkit.persistence.events.memory import _event_visible, _page, _public

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


class JsonlRunEventStore(RunEventStore):
    def __init__(self, base_dir: str, *, max_trace_content: int | None = None) -> None:
        self._base = Path(base_dir).resolve()
        self._max_trace_content = max_trace_content
        self._seq: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()
        return lock

    def _run_file(self, thread_id: str, run_id: str) -> Path:
        for value in (thread_id, run_id):
            if not SAFE_ID_PATTERN.match(value):
                raise ValueError(f"unsafe id for a filesystem path: {value!r}")
        return self._base / "threads" / thread_id / "runs" / f"{run_id}.jsonl"

    async def put(self, event: dict[str, Any]) -> None:
        await self.put_batch([event])

    async def put_batch(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        if not events:
            return []
        normalized = [normalize_event(event) for event in events]
        thread_id = same_thread(normalized)
        for event in normalized:
            self._run_file(event["thread_id"], event["run_id"])
        async with self._lock_for(thread_id):
            last = await self._current_seq(thread_id)
            created_at = iso_utc(datetime.now(UTC))
            # json_safe=True：这一行马上就要变成文件里的一行，返回给调用方的形状
            # 必须和以后从盘上读回来的**一致**（不是 JSON 原生值的 content 会被归一）。
            stored = [
                _public(event, seq=last + index + 1, created_at=created_at, json_safe=True)
                for index, event in enumerate(normalized)
            ]
            await asyncio.to_thread(self._append_files, stored)
            self._seq[thread_id] = stored[-1]["seq"]
            return stored

    async def put_if_absent(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = normalize_event(event)
        path = self._run_file(normalized["thread_id"], normalized["run_id"])
        async with self._lock_for(normalized["thread_id"]):
            existing = await asyncio.to_thread(self._find_in_file, path, normalized["event_type"])
            if existing is not None:
                return existing, False
            last = await self._current_seq(normalized["thread_id"])
            stored = _public(
                normalized, seq=last + 1, created_at=iso_utc(datetime.now(UTC)), json_safe=True
            )
            await asyncio.to_thread(self._append_files, [stored])
            self._seq[normalized["thread_id"]] = stored["seq"]
            return stored, True

    async def list_messages(
        self,
        thread_id: str,
        limit: int,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        self._run_file(thread_id, "scan")
        rows = await asyncio.to_thread(self._read_thread, thread_id)
        messages = [row for row in rows if row["category"] == "message"]
        return _page(messages, limit, before_seq, after_seq)

    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        event_types: Sequence[str] | None = None,
        after_seq: int | None = None,
        task_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        path = self._run_file(thread_id, run_id)
        rows = await asyncio.to_thread(self._read_file, path)
        visible = [row for row in rows if _event_visible(row, event_types, after_seq, task_id)]
        visible.sort(key=lambda row: row["seq"])
        if limit is not None:
            visible = visible[:limit]
        return visible

    async def count_messages(self, thread_id: str) -> int:
        self._run_file(thread_id, "scan")
        rows = await asyncio.to_thread(self._read_thread, thread_id)
        return sum(1 for row in rows if row["category"] == "message")

    async def _current_seq(self, thread_id: str) -> int:
        if thread_id not in self._seq:
            self._seq[thread_id] = await asyncio.to_thread(self._scan_max_seq, thread_id)
        return self._seq[thread_id]

    def _append_files(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[Path, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(self._run_file(row["thread_id"], row["run_id"]), []).append(row)
        temps: list[tuple[Path, Path]] = []
        try:
            for path, additions in grouped.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                previous = path.read_text(encoding="utf-8") if path.exists() else ""
                # 用 codec.dumps（带 default=）：一行里混进一个 LangChain 消息对象，
                # 以前整个 put_batch 会在这里 TypeError，而事件已经"写成功了"——
                # 表现是消息发出去、列表里什么都没有。
                payload = previous + "".join(dumps(row) + "\n" for row in additions)
                temporary = path.with_suffix(".jsonl.tmp")
                temporary.write_text(payload, encoding="utf-8")
                temps.append((temporary, path))
            for temporary, path in temps:
                os.replace(temporary, path)
        except Exception:
            for temporary, _path in temps:
                temporary.unlink(missing_ok=True)
            raise

    def _find_in_file(self, path: Path, event_type: str) -> dict[str, Any] | None:
        for row in self._read_file(path):
            if row["event_type"] == event_type:
                return row
        return None

    def _read_thread(self, thread_id: str) -> list[dict[str, Any]]:
        directory = self._base / "threads" / thread_id / "runs"
        if not directory.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.jsonl")):
            rows.extend(self._read_file(path))
        return rows

    def _read_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _scan_max_seq(self, thread_id: str) -> int:
        highest = 0
        for row in self._read_thread(thread_id):
            highest = max(highest, int(row["seq"]))
        return highest
