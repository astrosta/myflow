import os
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from agentkit.config.agents_config import AgentsConfig
from agentkit.config.checkpointer_config import CheckpointerConfig
from agentkit.config.database_config import DatabaseConfig
from agentkit.config.file_signature import ConfigSignature, get_config_signature
from agentkit.config.llm_call_config import LlmCallConfig
from agentkit.config.model_config import ModelConfig
from agentkit.config.reload_boundary import format_field_description
from agentkit.config.run_events_config import RunEventsConfig
from agentkit.config.runs_config import RunsConfig
from agentkit.config.runtime_paths import existing_project_file
from agentkit.config.sandbox_config import SandboxConfig
from agentkit.config.stream_config import StreamBridgeConfig
from agentkit.config.summarization_config import SummarizationConfig
from agentkit.config.title_config import TitleConfig
from agentkit.config.tool_config import ToolConfig


class AppConfig(BaseModel):
    config_version: int = Field(default=0)
    log_level: str = Field(default="info", description=format_field_description("log_level"))
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    max_recursion_limit: int = Field(
        default=1000,
        ge=1,
        description="Hard server-side ceiling for a client-supplied run recursion_limit. Client values above this are clamped; prevents runaway LangGraph super-steps (LLM cost / DoS).",
    )
    checkpointer: CheckpointerConfig = Field(default_factory=CheckpointerConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    runs: RunsConfig = Field(default_factory=RunsConfig)
    run_events: RunEventsConfig = Field(default_factory=RunEventsConfig)
    stream_bridge: StreamBridgeConfig = Field(default_factory=StreamBridgeConfig)
    llm_call: LlmCallConfig = Field(default_factory=LlmCallConfig)
    title: TitleConfig = Field(default_factory=TitleConfig)
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig)
    models: list[ModelConfig] = Field(default_factory=list)
    tools: list[ToolConfig] = Field(default_factory=list)
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
            by_name.setdefault(m.name, m)
        self._models_by_name = by_name
        return self

    @model_validator(mode="after")
    def _validate_agents_default_model(self) -> Self:
        name = self.agents.default_model
        if name is not None and name not in self._models_by_name:
            raise ValueError(
                f"agents.default_model {name!r} is not one of models[].name"
            )
        return self

    @model_validator(mode="after")
    def _validate_title_model(self) -> Self:
        name = self.title.model_name
        if name is not None and name not in self._models_by_name:
            raise ValueError(f"title.model_name {name!r} is not one of models[].name")
        summary_model = self.summarization.model_name
        if summary_model is not None and summary_model not in self._models_by_name:
            raise ValueError(f"summarization.model_name {summary_model!r} is not one of models[].name")
        return self

    @classmethod
    def resolve_config_path(cls, config_path: Path|None = None) -> Path:
        if config_path:
            if not Path.exists(Path(config_path)):
                raise FileNotFoundError(f"Config file {config_path} does not exist")
            return Path(config_path)
        elif env_config_path := os.getenv("MYFLOW_CONFIG_PATH"):
            if not Path.exists(Path(env_config_path)):
                raise FileNotFoundError(f"Config file {env_config_path} does not exist")
            return Path(env_config_path)
        elif file := existing_project_file(("config.yaml")):
            return file
        else:
            raise FileNotFoundError("No config file found. Please set the MYFLOW_CONFIG_PATH environment variable or provide a config file path.")
    
    @classmethod
    def from_file(cls, config_path: Path|None = None) -> Self:
        # 读取配置为年
        reslove_path = cls.resolve_config_path(config_path)
        with open(reslove_path, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}
        
        # $ENV展开
        config_data = cls.resolve_env_variables(config_data)

        result = cls.model_validate(config_data)
        if not result.models:
            raise ValueError("No models found in config")
        return result

    def get_model_config(self, name: str) -> ModelConfig | None:
        return self._models_by_name.get(name)


_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_signature: ConfigSignature | None = None
_app_config_is_custom = False                


def set_app_config(config: AppConfig) -> None:
    global _app_config, _app_config_path, _app_config_signature, _app_config_is_custom
    _app_config = config
    _app_config_path = None
    _app_config_signature = None
    _app_config_is_custom = True

def get_app_config() -> AppConfig:
    global _app_config, _app_config_path,  _app_config_signature, _app_config_is_custom

    # 1. 注入的 mock 永不被磁盘重载顶掉
    if _app_config is not None and _app_config_is_custom:
        return _app_config

    # 2. 路径或内容签名变了就重载
    resolved_path = AppConfig.resolve_config_path()
    current_signature = get_config_signature(resolved_path)
    should_reload = _app_config is None or _app_config_path != resolved_path or _app_config_signature != current_signature
    if should_reload:
        _load_and_cache_app_config(str(resolved_path))

    # 3. 返回缓存
    return _app_config

def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    """Load config from disk and refresh cache metadata."""
    global _app_config, _app_config_path,  _app_config_signature, _app_config_is_custom

    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_signature = get_config_signature(resolved_path)
    _app_config_is_custom = False
    return _app_config

