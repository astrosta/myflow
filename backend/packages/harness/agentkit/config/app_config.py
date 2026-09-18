from pydantic import BaseModel, Field, ConfigDict, PrivateAttr, model_validator
from typing import Any
from agentkit.config.model_config import ModelConfig
from agentkit.config.reload_boundary import format_field_description
import hashlib
import os

class AppConfig(BaseModel):
    config_version: int = Field(default=0)
    log_level: str = Field(default="info", description=format_field_description("log_level"))
    models: list[ModelConfig] = Field(default_factory=list)
    model_config = ConfigDict(extra="allow")
    _models_by_name: dict[str, ModelConfig] = PrivateAttr(default_factory=dict)

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        if isinstance(config, str):
            if config.startswith("$"):
                value = os.getenv(config[1:])
                if value is None:
                    raise ValueError(f"Environment variable {config} is not set")
                return value
            return config
        if isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        if isinstance(config, dict):
            return {key: cls.resolve_env_variables(value) for key, value in config.items()}
        return config

    @model_validator(mode="before")
    @classmethod
    def _drop_null_config_sections(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return{k: v for k, v in data.items() if v is not None}
        return data

    @model_validator(mode="after")
    def _build_name_indexes(self) -> "AppConfig":
        by_name: dict[str, ModelConfig] = {}
        for m in self.models:
            by_name[m.name] = m
        self._models_by_name = by_name
        return self