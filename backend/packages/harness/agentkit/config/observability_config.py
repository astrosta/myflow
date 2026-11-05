"""Langfuse observability configuration.

The whole section is frozen at process start: the Langfuse client, its exporter
threads, and the callback handler factory are built once by the gateway lifespan.
Editing YAML while the gateway is running must not silently point runs at a
different project.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ENVIRONMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MIN_HASH_SALT = 16


class LangfuseConfig(BaseModel):
    """Connection and content policy for a remote Langfuse instance."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = ""
    #: Secrets are expanded from the environment in config.example.yaml.
    #: Keep them out of repr() so a stray config log cannot leak the key.
    public_key: str = ""
    secret_key: str = Field(default="", repr=False)
    project_id: str = ""

    environment: str = "development"
    release: str = ""
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    timeout_seconds: int = Field(default=5, ge=1, le=60)

    #: redacted = keep inputs/outputs after irreversible PII masking.
    #: metadata_only = do not export prompts, completions or tool payloads.
    #: full = local-only escape hatch; still subject to the attribute size cap.
    capture_content: Literal["redacted", "metadata_only", "full"] = "redacted"
    user_id_mode: Literal["hashed", "raw", "none"] = "hashed"
    user_id_salt: str = Field(default="", repr=False)

    trace_subagents: bool = True
    flush_on_run_end: bool = False
    #: If true, startup fails when the Langfuse client cannot be constructed.
    #: Keep false by default: observability must not take the agent down.
    required: bool = False

    @model_validator(mode="after")
    def _validate_enabled_connection(self) -> "LangfuseConfig":
        if not self.enabled:
            return self

        for field_name in ("base_url", "public_key", "secret_key"):
            if not getattr(self, field_name).strip():
                raise ValueError(
                    f"observability.langfuse.enabled=true requires {field_name}"
                )

        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "observability.langfuse.base_url must be an http(s) URL "
                "including the host"
            )

        if self.user_id_mode == "hashed" and len(self.user_id_salt) < _MIN_HASH_SALT:
            raise ValueError(
                "observability.langfuse.user_id_mode=hashed requires "
                f"user_id_salt with at least {_MIN_HASH_SALT} characters"
            )

        if parsed.username or parsed.password:
            raise ValueError(
                "observability.langfuse.base_url must not embed credentials; "
                "use the public_key/secret_key fields"
            )

        if not _ENVIRONMENT_RE.fullmatch(self.environment):
            raise ValueError(
                "observability.langfuse.environment must match "
                "^[a-z0-9][a-z0-9_-]{0,39}$"
            )
        if self.environment.startswith("langfuse"):
            raise ValueError(
                "observability.langfuse.environment must not start with 'langfuse'"
            )
        return self

    @model_validator(mode="after")
    def _validate_base_url_when_set(self) -> "LangfuseConfig":
        if not self.base_url:
            return self
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "observability.langfuse.base_url must be an http(s) URL "
                "including the host"
            )
        return self


class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)


__all__ = ["LangfuseConfig", "ObservabilityConfig"]
