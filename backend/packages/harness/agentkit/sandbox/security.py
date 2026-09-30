"""Host-bash policy and virtual-path checks. Local sandbox is not an isolation boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agentkit.config import get_app_config

_DEFAULT_PROVIDER = "agentkit.sandbox.local:LocalSandboxProvider"
_REJECT_HINT = (
    "Path is outside the sandbox. Use an absolute virtual path under "
    "/mnt/user-data/workspace, /mnt/user-data/uploads, or /mnt/user-data/outputs. "
    "To probe the environment, run a command instead of a host path."
)
_MAX_DECODE_ROUNDS = 3


@dataclass(frozen=True)
class PathMapping:
    container_path: str
    local_path: str
    read_only: bool = False


def safe_app_config() -> Any | None:
    try:
        return get_app_config()
    except Exception:
        return None


def sandbox_settings(config: Any | None = None) -> Any | None:
    resolved = config if config is not None else safe_app_config()
    if resolved is None:
        return None
    return getattr(resolved, "sandbox", None)


def sandbox_use(config: Any | None = None) -> str:
    sandbox = sandbox_settings(config)
    if sandbox is None:
        return _DEFAULT_PROVIDER
    if isinstance(sandbox, dict):
        return str(sandbox.get("use") or _DEFAULT_PROVIDER)
    return str(getattr(sandbox, "use", None) or _DEFAULT_PROVIDER)


def sandbox_flag(name: str, default: Any, config: Any | None = None) -> Any:
    sandbox = sandbox_settings(config)
    if sandbox is None:
        return default
    if isinstance(sandbox, dict):
        return sandbox.get(name, default)
    return getattr(sandbox, name, default)


def uses_local_sandbox_provider(config: Any | None = None) -> bool:
    use = sandbox_use(config)
    return use == _DEFAULT_PROVIDER or use.endswith(":LocalSandboxProvider") or "sandbox.local" in use


def is_host_bash_allowed(config: Any | None = None) -> bool:
    """Isolating providers are unrestricted. Local bash stays off unless opted in."""
    resolved = config if config is not None else safe_app_config()
    if not uses_local_sandbox_provider(resolved):
        return True
    return bool(sandbox_flag("allow_host_bash", False, resolved))


def is_local_sandbox_id(sandbox_id: str) -> bool:
    return sandbox_id == "local" or sandbox_id.startswith("local:")


def find_path_mapping(virtual_path: str, mappings: list[PathMapping]) -> PathMapping | None:
    """Longest ``container_path`` wins. Match is segment-bounded."""
    for mapping in sorted(mappings, key=lambda item: len(item.container_path), reverse=True):
        prefix = mapping.container_path.rstrip("/")
        if virtual_path == prefix or virtual_path.startswith(prefix + "/"):
            return mapping
    return None


def _fully_unquote(value: str) -> str:
    current = value
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    return current


def resolve_sandbox_path(virtual_path: str, mappings: list[PathMapping]) -> Path:
    """Translate a virtual path to a host path, or raise ``PermissionError``.

    Order is load-bearing: pure string checks, then the mapping table, then
    filesystem ``resolve()``. The first group is the cheap reject path.

    ``..`` and symlink escape are both caught by ``is_relative_to`` after
    ``resolve()``. A string check for ``..`` is only a fast reject.

    Known gap: ``resolve()`` and the later ``open()`` are not atomic.
    """
    if "\x00" in virtual_path:
        raise PermissionError(_REJECT_HINT)

    decoded = _fully_unquote(virtual_path)
    if "\x00" in decoded:
        raise PermissionError(_REJECT_HINT)
    if "://" in decoded:
        raise PermissionError(_REJECT_HINT)
    if re.match(r"^[A-Za-z]:[\\/]", decoded) or decoded.startswith("\\\\") or decoded.startswith("//"):
        raise PermissionError(_REJECT_HINT)
    if not decoded.startswith("/"):
        raise PermissionError(_REJECT_HINT)
    if any(part == ".." for part in decoded.split("/")):
        raise PermissionError(_REJECT_HINT)

    mapping = find_path_mapping(decoded, mappings)
    if mapping is None:
        raise PermissionError(_REJECT_HINT)

    relative = decoded[len(mapping.container_path.rstrip("/")) :].lstrip("/")
    root = Path(mapping.local_path).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError(_REJECT_HINT)
    return candidate


def assert_writable(virtual_path: str, mappings: list[PathMapping]) -> Path:
    mapping = find_path_mapping(virtual_path, mappings)
    if mapping is not None and mapping.read_only:
        raise PermissionError(_REJECT_HINT)
    return resolve_sandbox_path(virtual_path, mappings)
