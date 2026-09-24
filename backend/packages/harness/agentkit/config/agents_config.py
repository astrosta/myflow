from pydantic import BaseModel, ConfigDict, Field


class AgentsConfig(BaseModel):
    """M1 agents section. Only ``default_model`` is read, and it hot-reloads."""

    model_config = ConfigDict(extra="forbid")

    default_model: str | None = Field(
        default=None,
        description="Per-run default model name; must be one of models[].name",
    )
