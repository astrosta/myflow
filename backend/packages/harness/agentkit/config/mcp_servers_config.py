"""`mcp_servers:` block: MyFlow-owned MCP servers, disabled by default.

```yaml
mcp_servers:
  enabled: true
  encryption_key: $MYFLOW_MCP_SERVER_KEY   # 32 url-safe base64 bytes
  request_timeout_seconds: 30
  connect_timeout_seconds: 5
  max_servers_per_user: 20
  allow_private_networks: false
  allowed_schemes: ["http", "https"]
```

Three things this block refuses to let through:

* an enabled block without a **valid** Fernet key.  The key encrypts every
  `mcp_servers.headers_encrypted`, so a half-configured deployment would write
  headers it can never decrypt.  `validate_for_startup()` is the startup
  pre-flight; it is deliberately not a model validator, so an operator can still
  build the object in order to inspect why it is wrong;
* an empty `allowed_schemes`.  Server URLs are checked against it before any
  outbound connection, so an empty list would mean "no URL is acceptable" while
  looking like "no restriction";
* a key copied from the auth/JWT secret.  Nothing mechanically stops an operator
  from doing that, so the error message says it out loud: rotating the JWT secret
  must never brick every stored MCP header.
"""

from __future__ import annotations

import os
import re

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

#: The deployment fact this block reads.  `config.yaml` writes
#: `encryption_key: $MYFLOW_MCP_SERVER_KEY`; an operator that exports the variable
#: instead of templating it is still served by `resolved_encryption_key()`.
MCP_SERVER_KEY_ENV = "MYFLOW_MCP_SERVER_KEY"

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


class McpServersConfig(BaseModel):
    """The `mcp_servers:` block.  Disabled means no MCP server is reachable."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "MCP 服务管理总开关。关闭时不连任何上游 MCP server，"
            "/api/mcp-servers 与 tool_search/tool_execute 全部不可用。"
        ),
    )
    encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "headers 的 Fernet 密钥（32 url-safe base64 字节），来自 "
            f"${MCP_SERVER_KEY_ENV}。不得复用 auth/JWT 密钥："
            "轮换它等于作废所有已存 header。"
        ),
    )
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, description="tools/call 单次请求超时。"
    )
    connect_timeout_seconds: float = Field(
        default=5.0, gt=0, description="initialize 建连超时；建连慢的 server 应在这里失败。"
    )
    max_servers_per_user: int = Field(
        default=20, ge=1, le=1000, description="每个用户可创建的 server 数量上限。"
    )
    allow_private_networks: bool = Field(
        default=False,
        description=(
            "是否允许 server URL 指向私网/loopback/link-local。这是 SSRF 边界，"
            "默认拒绝；前端勾选不算数。"
        ),
    )
    allowed_schemes: list[str] = Field(
        default_factory=lambda: ["http", "https"],
        description=(
            "server URL 允许的协议，默认 http/https。http 会明文传输 header，"
            "仅应在可信网络中使用；私网/loopback 仍由 allow_private_networks 控制。"
        ),
    )

    @field_validator("allowed_schemes")
    @classmethod
    def _schemes_are_bare_schemes(cls, value: list[str]) -> list[str]:
        """`https` is a scheme; `https://` is a URL someone meant to be a scheme."""
        schemes: list[str] = []
        for raw in value:
            text = str(raw or "").strip().lower()
            if not _SCHEME.fullmatch(text):
                raise ValueError(
                    "mcp_servers.allowed_schemes entries must be bare schemes such as "
                    f"'http' or 'https', got {raw!r}"
                )
            if text not in schemes:
                schemes.append(text)
        return schemes

    def resolved_encryption_key(self) -> str:
        """The configured key, else `MYFLOW_MCP_SERVER_KEY`; `""` when neither."""
        configured = self.encryption_key.get_secret_value().strip()
        return configured or os.getenv(MCP_SERVER_KEY_ENV, "").strip()

    def validate_for_startup(self) -> None:
        """Fail the boot, not the first server write."""
        if not self.allowed_schemes:
            raise ValueError(
                "mcp_servers.allowed_schemes must not be empty: every server URL is "
                "checked against it before connecting"
            )
        if not self.enabled:
            return
        key = self.resolved_encryption_key()
        if not key:
            raise ValueError(
                "mcp_servers.enabled=true requires mcp_servers.encryption_key "
                f"(set it from ${MCP_SERVER_KEY_ENV}); never reuse the auth or JWT secret"
            )
        if not _is_fernet_key(key):
            raise ValueError(
                "mcp_servers.encryption_key must be a valid Fernet key: 32 url-safe "
                "base64-encoded bytes (HeaderCipher.generate_key() produces one)"
            )


def _is_fernet_key(key: str) -> bool:
    """Fernet's own check, without echoing the key back in the error."""
    try:
        Fernet(key.encode("utf-8"))
    except ValueError:
        return False
    return True


__all__ = ["MCP_SERVER_KEY_ENV", "McpServersConfig"]
