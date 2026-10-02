"""Authority for event_type and category. Producers import these objects."""

from dataclasses import dataclass

from agentkit.runtime.events.constants import (
    RUN_EVENT_CATEGORY_MAX_LENGTH,
    RUN_EVENT_TYPE_MAX_LENGTH,
)


@dataclass(frozen=True, slots=True)
class RunEventDefinition:
    event_type: str
    category: str

    def __post_init__(self) -> None:
        if len(self.event_type) > RUN_EVENT_TYPE_MAX_LENGTH:
            raise ValueError(
                f"event_type {self.event_type!r} exceeds {RUN_EVENT_TYPE_MAX_LENGTH} characters"
            )
        if len(self.category) > RUN_EVENT_CATEGORY_MAX_LENGTH:
            raise ValueError(
                f"category {self.category!r} exceeds {RUN_EVENT_CATEGORY_MAX_LENGTH} characters"
            )


RUN_START_EVENT = RunEventDefinition("run.start", "trace")
RUN_END_EVENT = RunEventDefinition("run.end", "outputs")
RUN_ERROR_EVENT = RunEventDefinition("run.error", "error")
LLM_HUMAN_INPUT = RunEventDefinition("llm.human.input", "message")
LLM_AI_RESPONSE = RunEventDefinition("llm.ai.response", "message")
LLM_TOOL_RESULT = RunEventDefinition("llm.tool.result", "message")
LLM_ERROR_EVENT = RunEventDefinition("llm.error", "trace")
