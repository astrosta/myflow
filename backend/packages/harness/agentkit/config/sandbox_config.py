import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentkit.config.reload_boundary import format_field_description

DEFAULT_OPENSANDBOX_IMAGE = "python:3.12"
"""Default sandbox image.

OpenSandbox runs any image that has a shell, because only bash and the file API
are used: no browser, no Jupyter, no AIO runtime. A stock python image is enough
and keeps the pull small.
"""

_ENV_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


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


class RunCodeConfig(BaseModel):
    """The run_code tool. Disabled by default: no tool means no wasted turn."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Expose the run_code tool to the model.",
    )
    default_language: str = Field(default="python")
    timeout_seconds: float = Field(default=120, gt=0)


class OpenSandboxRunCodeConfig(BaseModel):
    """Code Interpreter image for the OpenSandbox provider.

    Set it when snippets should run in a real interpreter: the sandbox is then
    created from this image with this entrypoint, and run_code goes through
    CodeInterpreterSync instead of the shell.
    """

    model_config = ConfigDict(extra="forbid")

    image: str = Field(
        default=(
            "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/"
            "code-interpreter:v1.1.0"
        )
    )
    entrypoint: list[str] = Field(
        default_factory=lambda: ["/opt/code-interpreter/code-interpreter.sh"]
    )
    ready_timeout_seconds: float = Field(default=30, gt=0)


class OpenSandboxConfig(BaseModel):
    """OpenSandbox provider (agentkit.sandbox.opensandbox).

    The sandbox server owns containers; this section only says how to reach it
    and what to ask for. There is no port, no container name and no docker
    topology here: those live in the server's runtime configuration.
    """

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(
        default="127.0.0.1:8080",
        description="opensandbox-server address, host:port.",
    )
    protocol: Literal["http", "https"] = "http"
    api_key: str = Field(
        default="",
        description="Leave empty to read OPEN_SANDBOX_API_KEY; redact in logs and errors.",
    )
    image: str = DEFAULT_OPENSANDBOX_IMAGE
    timeout_seconds: int = Field(
        default=1800,
        gt=0,
        description="Sandbox TTL; the server reaps it after this long.",
    )
    ready_timeout_seconds: float = Field(default=30, gt=0)
    request_timeout_seconds: float = Field(default=30, gt=0)
    acquire_timeout_seconds: float = Field(
        default=120,
        gt=0,
        description="How long acquire waits for the per-thread Redis lease.",
    )
    resource: dict[str, str] = Field(
        default_factory=lambda: {"cpu": "2", "memory": "2Gi"},
        description="Server-side resource limits, e.g. {cpu: '2', memory: '2Gi'}.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    release_action: Literal["keep", "destroy"] = Field(
        default="keep",
        description="keep leaves the sandbox to its TTL; destroy kills it on release.",
    )
    max_cached_threads: int = Field(default=64, ge=1)
    discover_existing: bool = True
    startup_healthcheck: bool = True
    host_mount: HostMountConfig
    lease: SandboxLeaseConfig
    orphan_policy: Literal["report", "remove"] = "report"
    orphan_ttl_seconds: int = Field(default=86400, ge=0)
    run_code: OpenSandboxRunCodeConfig | None = Field(
        default=None,
        description="Set to run snippets in the Code Interpreter instead of the shell.",
    )

    @field_validator("domain", "image")
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
    run_code: RunCodeConfig = Field(
        default_factory=RunCodeConfig,
        description=format_field_description(
            "sandbox.run_code",
            field_doc="run_code tool: expose it, pick the default language and the wall clock.",
        ),
    )
    cube: CubeSandboxConfig | None = Field(
        default=None,
        description=format_field_description(
            "sandbox.cube",
            field_doc="CubeSandbox control-plane, host mount, and lease settings.",
        ),
    )
    opensandbox: OpenSandboxConfig | None = Field(
        default=None,
        description=format_field_description(
            "sandbox.opensandbox",
            field_doc="OpenSandbox server, image, host mount, and lease settings.",
        ),
    )


__all__ = [
    "DEFAULT_OPENSANDBOX_IMAGE",
    "CubeSandboxConfig",
    "CubeSandboxHostMountConfig",
    "CubeSandboxLeaseConfig",
    "CubeSandboxNetworkConfig",
    "HostMountConfig",
    "OpenSandboxConfig",
    "OpenSandboxRunCodeConfig",
    "RunCodeConfig",
    "SandboxConfig",
    "SandboxLeaseConfig",
]
