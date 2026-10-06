import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ContextSizeType = Literal["fraction", "tokens", "messages"]
DEFAULT_KEEP: tuple[ContextSizeType, int] = ("messages", 20)
DEFAULT_SKILL_FILE_READ_TOOL_NAMES: tuple[str, ...] = ("read_file", "read", "view", "cat")


class ContextSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ContextSizeType
    value: int | float

    @model_validator(mode="after")
    def _validate_value_range(self) -> "ContextSize":
        if isinstance(self.value, bool) or not math.isfinite(self.value):
            raise ValueError(f"ContextSize value must be finite (got {self.value!r})")
        if self.type == "fraction":
            if not 0 < self.value <= 1:
                raise ValueError(
                    f"fraction ContextSize value must be in (0, 1] (got {self.value!r}) — write 0.8 for 80%, not 80"
                )
        elif self.type == "messages" and not isinstance(self.value, int):
            raise ValueError(f"messages ContextSize value must be a whole number (got {self.value!r})")
        elif self.value <= 0:
            raise ValueError(f"{self.type} ContextSize value must be positive (got {self.value!r})")
        return self

    def to_tuple(self) -> tuple[ContextSizeType, int | float]:
        return (self.type, self.value)


class SummarizationConfig(BaseModel):
    """Off by default. A missing context window must not guess a trigger."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    model_name: str | None = None
    trigger: ContextSize | list[ContextSize] | None = None
    keep: ContextSize = Field(default_factory=lambda: ContextSize(type="messages", value=DEFAULT_KEEP[1]))
    trim_tokens_to_summarize: int | None = 4000
    summary_prompt: str | None = None
    skill_file_read_tool_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKILL_FILE_READ_TOOL_NAMES)
    )
