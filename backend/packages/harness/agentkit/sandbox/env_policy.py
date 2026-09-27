"""Sandbox process environment. Strip inherited secrets, then apply explicit extras."""

from __future__ import annotations

import os

_SECRET_NAME_PATTERNS = ("KEY", "SECRET", "TOKEN", "PASS", "CREDENTIAL")


def build_sandbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Drop inherited variables whose names look like secrets, then overlay ``extra``.

    Benign variables (``PATH``, ``HOME``, ``LANG``, ``VIRTUAL_ENV``) stay.
    Name matching is a coarse heuristic; it blocks the common leak for almost no cost.
    ``extra`` may reintroduce a request-scoped secret on purpose (M9).
    """
    base = {
        key: value
        for key, value in os.environ.items()
        if not any(pattern in key.upper() for pattern in _SECRET_NAME_PATTERNS)
    }
    base.update(extra or {})
    return base
