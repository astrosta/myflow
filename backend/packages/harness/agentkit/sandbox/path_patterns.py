"""Reverse-mask host paths in tool output. Dynamic roots are scanned, not compiled."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from agentkit.config.runtime_paths import runtime_home

if TYPE_CHECKING:
    from agentkit.sandbox.sandbox import Sandbox

_STABLE_PATTERNS: list[tuple[re.Pattern[str], str]] | None = None


def _stable_patterns() -> list[tuple[re.Pattern[str], str]]:
    """Compile only process-stable roots. Per-thread roots must not enter ``re`` cache."""
    global _STABLE_PATTERNS
    if _STABLE_PATTERNS is None:
        skills = runtime_home() / "skills"
        integrations = runtime_home() / "integrations"
        compiled: list[tuple[re.Pattern[str], str]] = []
        for directory, virtual in ((skills, "/mnt/skills"), (integrations, "/mnt/integrations")):
            text = str(directory)
            compiled.append((re.compile(re.escape(text)), virtual))
            if os.name == "nt":
                compiled.append((re.compile(re.escape(text.replace("\\", "/"))), virtual))
        _STABLE_PATTERNS = compiled
    return _STABLE_PATTERNS


def _replacements(sandbox: Sandbox | None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    mappings = getattr(sandbox, "path_mappings", ()) if sandbox is not None else ()
    for mapping in mappings:
        local = str(mapping.local_path)
        pairs.append((local, mapping.container_path))
        if os.name == "nt":
            pairs.append((local.replace("\\", "/"), mapping.container_path))
    home = str(runtime_home())
    pairs.append((home, "/mnt"))
    if os.name == "nt":
        pairs.append((home.replace("\\", "/"), "/mnt"))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def mask_host_paths(text: str, sandbox: Sandbox | None = None) -> str:
    """Replace host paths with virtual paths. Longer local paths win."""
    if not text:
        return text
    masked = text
    for local, virtual in _replacements(sandbox):
        if local and local in masked:
            masked = masked.replace(local, virtual)
    for pattern, virtual in _stable_patterns():
        masked = pattern.sub(virtual, masked)
    return masked
