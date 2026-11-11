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


def _host_only(value: str, field: str) -> str:
    """Validate a rewrite destination: a host or IP, never a URL or host:port."""

    text = str(value).strip()
    if not text:
        return ""
    if "://" in text or "/" in text or any(ch.isspace() for ch in text):
        raise ValueError(f"{field} must be a host without a scheme or path: {value!r}")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text.count(":") == 1:
        raise ValueError(f"{field} must be a host, not host:port: {value!r}")
    return text


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


class E2BSandboxNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_out: list[str] = Field(default_factory=list)
    deny_out: list[str] = Field(default_factory=list)
    rules: dict | None = None


class E2BSandboxSyncConfig(BaseModel):
    """Explicit host<->sandbox file sync; the E2B protocol has no host mounts.

    Acquire uploads the host uploads/ tree, release downloads outputs/. Skill
    files are uploaded lazily when a tool first reads them, so upload_skills
    only turns that source on; it never bulk-copies the tree.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    upload_uploads: bool = True
    upload_workspace: bool = False
    upload_skills: bool = True
    download_outputs: bool = True
    download_workspace: bool = False
    max_file_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    max_tree_bytes: int = Field(default=256 * 1024 * 1024, gt=0)


class E2BSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://127.0.0.1:3000"
    api_key: str = Field(
        default="",
        description="Leave empty to read E2B_API_KEY; redact in logs and errors",
    )
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
    network: E2BSandboxNetworkConfig = Field(default_factory=E2BSandboxNetworkConfig)
    lease: SandboxLeaseConfig
    sync: E2BSandboxSyncConfig = Field(default_factory=E2BSandboxSyncConfig)
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
    endpoint_host: str = Field(
        default="",
        description=(
            "Host the Gateway uses for direct sandbox data-plane endpoints. "
            "Empty keeps the server-authored host; set 127.0.0.1 when the "
            "Gateway runs on the Docker host and the server returns "
            "host.docker.internal. Do not include a scheme or path."
        ),
    )
    endpoint_host_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-host rewrite for server-authored endpoints, e.g. "
            "{host.docker.internal: 127.0.0.1}. Keys may be host or host:port; "
            "values are hosts and keep the original port/path."
        ),
    )
    use_server_proxy: bool = Field(
        default=False,
        description=(
            "Route data-plane traffic through the server proxy (deprecated in "
            "the OpenSandbox SDK). Leave false for direct endpoints."
        ),
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
    prewarm: bool = Field(
        default=True,
        description=(
            "Speculatively acquire the thread sandbox while the model is "
            "generating, so the first model token never waits for container "
            "startup. Tools still acquire lazily and fall back on failure."
        ),
    )
    prewarm_max_concurrent: int = Field(
        default=1,
        ge=1,
        description="Maximum in-flight speculative acquisitions per Gateway process.",
    )
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

    @field_validator("endpoint_host")
    @classmethod
    def _endpoint_host_shape(cls, value: str) -> str:
        return _host_only(value, "endpoint_host")

    @field_validator("endpoint_host_map")
    @classmethod
    def _endpoint_host_map_shape(cls, value: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for source, target in value.items():
            key = str(source).strip().lower()
            if not key or "://" in key or "/" in key or any(ch.isspace() for ch in key):
                raise ValueError(
                    f"endpoint_host_map key must be host or host:port: {source!r}"
                )
            host = _host_only(str(target), f"endpoint_host_map[{source!r}]")
            if not host:
                raise ValueError(f"endpoint_host_map[{source!r}] must not be blank")
            result[key] = host
        return result

    @field_validator("env")
    @classmethod
    def _env_keys(cls, value: dict[str, str]) -> dict[str, str]:
        pattern = re.compile(_ENV_KEY_PATTERN)
        for key in value:
            if not pattern.match(key):
                raise ValueError(f"illegal environment variable name: {key!r}")
        return value


#: bash 输出返回给模型前的字符上限。provider 的 1 MiB 只是防失控，
#: 对上下文来说仍然太大；这里保留头尾，明确告诉模型有截断。
DEFAULT_BASH_MAX_OUTPUT_CHARS = 20_000


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
    bash_max_output_chars: int = Field(
        default=DEFAULT_BASH_MAX_OUTPUT_CHARS,
        ge=1000,
        description=(
            "Maximum bash output characters returned to the model. Longer output keeps "
            "the head and tail and includes an explicit truncation marker."
        ),
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
    e2b: E2BSandboxConfig | None = Field(
        default=None,
        description=format_field_description(
            "sandbox.e2b",
            field_doc="E2B control-plane, explicit file sync, and lease settings.",
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
    "DEFAULT_BASH_MAX_OUTPUT_CHARS",
    "DEFAULT_OPENSANDBOX_IMAGE",
    "E2BSandboxConfig",
    "E2BSandboxNetworkConfig",
    "E2BSandboxSyncConfig",
    "HostMountConfig",
    "OpenSandboxConfig",
    "OpenSandboxRunCodeConfig",
    "RunCodeConfig",
    "SandboxConfig",
    "SandboxLeaseConfig",
]
