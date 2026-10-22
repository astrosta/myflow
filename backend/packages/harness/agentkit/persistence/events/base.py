"""Run-event ledger interface. Callers depend on this, not on a backend."""

import abc
from collections.abc import Sequence
from typing import Any


class RunEventSeqConflict(RuntimeError):
    """Seq allocation still collided after the retry budget."""


class RunEventStore(abc.ABC):
    @abc.abstractmethod
    async def put(self, event: dict[str, Any]) -> None:
        """低频路径。正常执行用 put_batch。"""

    @abc.abstractmethod
    async def put_batch(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """同 thread 的一批事件。返回与传入等长、同序、seq 连续。"""

    @abc.abstractmethod
    async def put_if_absent(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """终态回执。created=False 表示同 run 的这个 event_type 已经有人写过。"""

    @abc.abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        limit: int,
        before_seq: int | None = None,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """只返回 category=message，并且按 seq 升序。"""

    @abc.abstractmethod
    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        event_types: Sequence[str] | None = None,
        after_seq: int | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """该 run 的全部类别，按 seq 升序。

        ``task_id``（M8 §14.3）按 ``metadata["task_id"]`` 过滤：展开一张子代理卡片时
        只拉那一条委派的 ``subagent.*`` 事件。过滤骑在已有的
        ``(thread_id, run_id, seq)`` 索引上，**不改 schema**。
        """

    @abc.abstractmethod
    async def count_messages(self, thread_id: str) -> int:
        """How many message rows this thread already has. The checkpoint seed reads this once."""
