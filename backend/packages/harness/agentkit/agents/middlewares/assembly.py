from langchain.agents.middleware import AgentMiddleware
from agentkit.config import AppConfig
from langchain_core.runnables import RunnableConfig

def build_runtime_middlewares(
    app_config: AppConfig,
    *,
    include_uploads: bool,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    pass

def build_middlewares(
    config: RunnableConfig,
    model_name: str | None,
    *,
    app_config: AppConfig | None = None,
    custom_middlewares: list[AgentMiddleware],
) -> list[AgentMiddleware]:
    pass

def build_subagent_runtime_middlewares(
    app_config: AppConfig,
    *,
    model_name: str | None = None,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    pass