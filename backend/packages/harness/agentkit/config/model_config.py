from pydantic import BaseModel, Field

class ModelConfig(BaseModel):
    provider_type: str = Field(default="deepseek", description="The type of the model provider")
    model: str = Field(default="deepseek-v4-flash", description="The model to use")
    base_url: str = Field(default="https://api.deepseek.com", description="The base URL for the model provider")
    api_key: str = Field(default="$DEEPSEEK_API_KEY", description="The API key for the model provider")
