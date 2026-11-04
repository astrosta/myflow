from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.reload_boundary import format_field_description


class CubeSandboxNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_out: list[str] = Field(default_factory=list)
    deny_out: list[str] = Field(default_factory=list)
    rules: dict | None = None


class CubeSandboxHostMountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_root: str | None = None
    host_root: str | None = None
    allowed_prefix: str | None = None


class CubeSandboxLeaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redis_url: str
    ttl_seconds: int = 30


class CubeSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://127.0.0.1:3000"
    api_key: str = Field(default="", description="Leave empty to read E2B_API_KEY / CUBE_API_KEY; redact in logs and errors")
    template_id: str
    sandbox_domain: str = "cube.app"
    timeout_seconds: int = 1800
    on_timeout: Literal["kill", "pause"] = "pause"
    auto_resume: bool = True
    release_action: Literal["pause", "kill"] = "pause"
    acquire_timeout_seconds: float = 120
    request_timeout_seconds: float = 30
    startup_healthcheck: bool = True
    allow_internet_access: bool = True
    allow_public_traffic: bool = False
    sandbox_user: str = "root"
    network: CubeSandboxNetworkConfig = Field(default_factory=CubeSandboxNetworkConfig)
    host_mount: CubeSandboxHostMountConfig
    lease: CubeSandboxLeaseConfig
    max_cached_threads: int = 64


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
    cube: CubeSandboxConfig | None = Field(
        default=None,
        description=format_field_description(
            "sandbox.cube",
            field_doc="CubeSandbox control-plane, host mount, and lease settings.",
        ),
    )
