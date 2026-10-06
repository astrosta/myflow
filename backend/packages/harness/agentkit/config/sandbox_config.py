from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.reload_boundary import format_field_description


class SandboxConfig(BaseModel):
    """Local sandbox is path mapping, not isolation. Host bash stays off unless opted in."""

    model_config = ConfigDict(extra="forbid")

    use: str = Field(
        default="agentkit.sandbox.local:LocalSandboxProvider",
        description=format_field_description(
            "sandbox.use",
            field_doc="Class path of the sandbox provider. The process singleton is created on first acquire.",
        ),
    )
    allow_host_bash: bool = Field(
        default=False,
        description="When the local provider is in use, expose bash tools only if this is true.",
    )
    bash_command_timeout: float = Field(
        default=600,
        gt=0,
        description="Wall-clock timeout in seconds for sandbox bash commands.",
    )
    mounts: list[dict] = Field(
        default_factory=list,
        description="Extra process-wide mounts as {container_path, local_path, read_only}.",
    )
