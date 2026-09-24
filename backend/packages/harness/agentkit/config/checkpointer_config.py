from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.reload_boundary import format_field_description


class CheckpointerConfig(BaseModel):
    """M1 checkpointer section. Only ``type: memory`` is accepted."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["memory","sqlite", "postgres"] = Field(
        default="memory",
        description=format_field_description("checkpointer.type"),
    )
