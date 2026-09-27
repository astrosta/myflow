from pydantic import BaseModel, ConfigDict, Field

class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(default="default", description="Logical model name used by the factory")
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
    when_thinking_enabled: dict | None = Field(default=None, description="Extra settings to be passed to the model when thinking is enabled")
    when_thinking_disabled: dict | None = Field(default=None, description="Extra settings to be passed to the model when thinking is disabled")
    thinking: dict | None = Field(default=None, description="Shortcut thinking settings merged into when_thinking_enabled")
    pricing: dict | None = Field(default=None, description="Pricing metadata; never forwarded to the provider client")
    context_window: int | None = Field(default=None, gt=0, description="The context window for the model")
    max_tokens: int | None = Field(default=None, gt=0, description="The maximum number of tokens for the model")
    
