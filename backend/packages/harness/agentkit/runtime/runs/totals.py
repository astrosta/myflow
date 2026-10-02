"""Token snapshot copied onto a live run and onto the runs row."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunTotals:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        attributed = self.lead_agent_tokens + self.subagent_tokens + self.middleware_tokens
        total = self.total_input_tokens + self.total_output_tokens
        if attributed != total:
            logger.error("Token attribution %d does not add up to total %d", attributed, total)
