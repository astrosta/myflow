from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EFFORT_ID = r"^[A-Za-z0-9._-]{1,32}$"


class ReasoningEffortOption(BaseModel):
    """One selectable reasoning effort. `id` is the value forwarded to the provider."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=EFFORT_ID, description="Value sent to the provider (e.g. low/medium/high)")
    label: str | None = Field(default=None, description="UI label; falls back to id")
    description: str | None = Field(default=None, description="One line shown under the label")
    default: bool = Field(default=False, description="Preselected in the composer for this model")

    @field_validator("label", "description")
    @classmethod
    def _blank_becomes_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @property
    def display(self) -> str:
        return self.label or self.id


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(
        default="default",
        pattern=r"^[A-Za-z0-9._-]{1,128}$",
        description="Logical model name used by the factory",
    )
    display_name: str | None = Field(default=None, description="Human-readable model name")
    description: str | None = Field(default=None, description="Model description")
  #  provider_type: str = Field(default="deepseek", description="The type of the model provider")
    model: str = Field(default="deepseek-v4-flash", description="The model to use")
    base_url: str | None = Field(default="https://api.deepseek.com", description="The base URL for the model provider")
    use: str = Field(..., description="Class path of the model provider(e.g. langchain_openai.ChatOpenAI)",)
    api_key: str = Field(default="$DEEPSEEK_API_KEY", description="The API key for the model provider")
    use_response_api: bool | None = Field(default=None, description="Whether to use the response API")
    supports_thinking: bool = Field(default=False, description="Whether the model supports thinking")
    supports_reasoning_effort: bool = Field(default=False, description="Whether the model supports reasoning effort")
    supports_vision: bool = Field(default=False, description="Whether the model supports vision")
    reasoning_efforts: list[ReasoningEffortOption] = Field(
        default_factory=list,
        description="推理等级候选：id 是发给 provider 的取值，label/description/default 供前端展示",
    )
    when_thinking_enabled: dict | None = Field(default=None, description="Extra settings to be passed to the model when thinking is enabled")
    when_thinking_disabled: dict | None = Field(default=None, description="Extra settings to be passed to the model when thinking is disabled")
    thinking: dict | None = Field(default=None, description="Shortcut thinking settings merged into when_thinking_enabled")
    pricing: dict | None = Field(default=None, description="Pricing metadata; never forwarded to the provider client")
    context_window: int | None = Field(default=None, gt=0, description="The context window for the model")
    max_tokens: int | None = Field(default=None, gt=0, description="The maximum number of tokens for the model")

    @field_validator("reasoning_efforts", mode="before")
    @classmethod
    def _accept_plain_strings(cls, value: object) -> object:
        """`- low` 和 `- {id: low, label: 低}` 两种写法都要能用。"""

        if not isinstance(value, list):
            return value
        return [{"id": item} if isinstance(item, str) else item for item in value]

    @model_validator(mode="after")
    def _efforts_are_usable(self) -> Self:
        seen: set[str] = set()
        for option in self.reasoning_efforts:
            if option.id in seen:
                raise ValueError(f"models[].reasoning_efforts has a duplicate id {option.id!r}")
            seen.add(option.id)
        defaults = [option for option in self.reasoning_efforts if option.default]
        if len(defaults) > 1:
            raise ValueError(
                f"models[].reasoning_efforts has {len(defaults)} defaults; keep at most one"
            )
        # 公布了等级就等于支持：少写一个开关不该让前端拿不到候选
        if self.reasoning_efforts:
            self.supports_reasoning_effort = True
        return self

    @property
    def default_reasoning_effort(self) -> str | None:
        """只有**显式标了** default 的那一档；没有就是 None（= 用 provider 默认值）。

        不取第一档是有意的：配了候选不等于推荐某一档，前端在没有默认值时显示
        “默认（不指定）”，于是不选就不下发 `reasoning_effort`，行为跟没配一样。
        """

        for option in self.reasoning_efforts:
            if option.default:
                return option.id
        return None

    def reasoning_effort_ids(self) -> list[str]:
        return [option.id for option in self.reasoning_efforts]
