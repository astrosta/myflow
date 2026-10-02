"""Run-event backend selection. Frozen when the store is built at startup."""

from typing import Literal

from pydantic import BaseModel


class RunEventsConfig(BaseModel):
    """`backend=memory` is the default so history is not persisted until asked for."""

    backend: Literal["memory", "db", "jsonl"] = "memory"
    max_trace_content: int = 10240
    track_token_usage: bool = True
    batch_size: int = 20
    flush_interval: float = 5.0
