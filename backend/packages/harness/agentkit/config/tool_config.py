from pydantic import BaseModel, ConfigDict, Field


class ToolConfig(BaseModel):
    """One configured tool. ``use`` is a variable path, not a class path."""

    name: str
    group: str = Field(description="Logical group used by the groups filter")
    use: str = Field(description="Variable path, for example agentkit.sandbox.tools:bash_tool")
    model_config = ConfigDict(extra="allow")
