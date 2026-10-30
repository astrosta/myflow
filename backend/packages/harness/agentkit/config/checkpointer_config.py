from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.reload_boundary import format_field_description


class CheckpointerConfig(BaseModel):
    """Sync checkpointer selection used by AgentClient / CLI.

    The Gateway picks its checkpointer from ``database.backend`` instead.
    ``postgres`` here also requires ``database.backend: postgres`` and
    ``database.url``; the sync provider reuses the same libpq DSN as the ORM.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["memory","sqlite", "postgres"] = Field(
        default="memory",
        description=format_field_description("checkpointer.type"),
    )
