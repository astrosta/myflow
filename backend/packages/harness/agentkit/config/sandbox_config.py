import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentkit.config.reload_boundary import format_field_description

DEFAULT_AIO_IMAGE = (
    "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0"
)
"""Pinned AIO sandbox image.

The floating latest tag can still sit on the 1.0.0.x line, which predates the
/v1/bash namespace: no per-command env injection, no server-side hard timeout.
1.11.0 is the top of the line upstream recommends (1.9.3 through 1.11.0).
"""

_ENV_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_CONTAINER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class HostMountConfig(BaseModel):
    """Shared host-mount section: what the Gateway sees vs what the runtime sees."""

    model_config = ConfigDict(extra="forbid")

    local_root: str | None = None
    host_root: str | None = None
    allowed_prefix: str | None = None


class SandboxLeaseConfig(BaseModel):
    """Shared Redis per-thread lease section."""

    model_config = ConfigDict(extra="forbid")

    redis_url: str
    ttl_seconds: int = 30


# The CubeSandbox sections predate the shared models; keep their names importable.
CubeSandboxHostMountConfig = HostMountConfig
CubeSandboxLeaseConfig = SandboxLeaseConfig


class CubeSandboxNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_out: list[str] = Field(default_factory=list)
    deny_out: list[str] = Field(default_factory=list)
    rules: dict | None = None


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


class AioSandboxNetworkConfig(BaseModel):
    """Docker network for the sandbox container."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["bridge", "none"] = "bridge"
    name: str | None = Field(
        default=None,
        description="Existing Docker network name; empty means the default bridge.",
    )


class AioSandboxResourcesConfig(BaseModel):
    """Container resource caps. An empty value means "do not pass the flag"."""

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = Field(default=None, gt=0)
    memory: str | None = Field(default=None, description="Docker memory limit, e.g. 2g")
    pids_limit: int | None = Field(default=None, gt=0)


class AioSandboxConfig(BaseModel):
    """AIO sandbox container provider (agentkit.sandbox.aio_sandbox)."""

    model_config = ConfigDict(extra="forbid")

    image: str = DEFAULT_AIO_IMAGE
    pull_policy: Literal["missing", "always"] = "missing"
    container_name_prefix: str = Field(
        default="myflow-aio", pattern=_CONTAINER_NAME_PATTERN
    )
    port: int = Field(default=8080, gt=0, lt=65536)
    publish_host: str = Field(
        default="auto",
        description="auto | 127.0.0.1 | 0.0.0.0 | <ip>; the host address the port binds to.",
    )
    connect_host: str = Field(
        default="auto",
        description="auto | 127.0.0.1 | host.docker.internal | <ip>; how the Gateway reaches that port.",
    )
    ready_timeout_seconds: float = Field(default=60, gt=0)
    request_timeout_seconds: float = Field(default=30, gt=0)
    release_action: Literal["stop", "remove"] = "stop"
    max_cached_threads: int = Field(default=64, ge=1)
    discover_existing: bool = True
    startup_healthcheck: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    network: AioSandboxNetworkConfig = Field(default_factory=AioSandboxNetworkConfig)
    resources: AioSandboxResourcesConfig = Field(
        default_factory=AioSandboxResourcesConfig
    )
    host_mount: HostMountConfig
    lease: SandboxLeaseConfig
    orphan_policy: Literal["report", "remove"] = "report"
    orphan_ttl_seconds: int = Field(default=86400, ge=0)

    @field_validator("image", "publish_host", "connect_host")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("env")
    @classmethod
    def _env_keys(cls, value: dict[str, str]) -> dict[str, str]:
        pattern = re.compile(_ENV_KEY_PATTERN)
        for key in value:
            if not pattern.match(key):
                raise ValueError(f"illegal environment variable name: {key!r}")
        return value


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
    aio: AioSandboxConfig | None = Field(
        default=None,
        description=format_field_description(
            "sandbox.aio",
            field_doc="AIO sandbox (Docker container) image, container, host mount, and lease settings.",
        ),
    )


__all__ = [
    "DEFAULT_AIO_IMAGE",
    "AioSandboxConfig",
    "AioSandboxNetworkConfig",
    "AioSandboxResourcesConfig",
    "CubeSandboxConfig",
    "CubeSandboxHostMountConfig",
    "CubeSandboxLeaseConfig",
    "CubeSandboxNetworkConfig",
    "HostMountConfig",
    "SandboxConfig",
    "SandboxLeaseConfig",
]
