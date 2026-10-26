"""Content-free memory metrics.

Logs and counters carry kinds, statuses, counts and latency only; never memory
content, evidence quotes, transcript text, Skill bodies, or PII.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any


class MemoryMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._observations: dict[str, list[float]] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += int(amount)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._observations.setdefault(name, []).append(float(value))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = dict(self._counters)
            for name, values in self._observations.items():
                if not values:
                    continue
                ordered = sorted(values)
                out[name + "_count"] = len(values)
                out[name + "_avg"] = sum(values) / len(values)
                out[name + "_p95"] = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            return out
