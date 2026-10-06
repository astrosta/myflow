from pydantic import BaseModel, ConfigDict, Field


class LlmCallConfig(BaseModel):
    """Retry budget for provider failures. Copied onto the middleware at construction."""

    model_config = ConfigDict(extra="forbid")

    retry_max_attempts: int = Field(default=3, ge=1, description="Attempts including the first call")
    retry_base_delay_ms: int = Field(default=1000, ge=0)
    retry_cap_delay_ms: int = Field(default=8000, ge=0)
