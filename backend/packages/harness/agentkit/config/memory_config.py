"""Long-term-memory configuration.

The legacy canonical runtime has been removed. The only memory configuration
that remains is the explicit OpenViking switch and its runtime settings.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.openviking_memory import OpenVikingMemoryConfig


class MemoryConfig(BaseModel):
    """memory: block in config.yaml."""

    model_config = ConfigDict(extra="forbid")

    openviking: OpenVikingMemoryConfig = Field(
        default_factory=lambda: OpenVikingMemoryConfig(enabled=False)
    )


__all__ = ["MemoryConfig"]
