"""Discover user-facing deliverables under a thread's outputs directory.

Only files under the host outputs directory are exposed as artifacts. Workspace
files are scratch space: they are readable by the agent but the artifact route
deliberately refuses to serve them.
"""

from __future__ import annotations

from pathlib import Path

from agentkit.config.paths import Paths, get_paths

OUTPUTS_VIRTUAL = "/mnt/user-data/outputs"

#: Safety cap: a runaway agent must not make a state response unbounded.
MAX_OUTPUT_ARTIFACTS = 1000


def _walk_files(directory: Path) -> list[Path]:
    """Walk regular files without following symlinks or staging partials."""

    found: list[Path] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.is_symlink():
                # A link can point outside outputs; the download route would
                # reject it anyway, so never advertise it as an artifact.
                continue
            if entry.is_dir():
                found.extend(_walk_files(entry))
            elif entry.is_file() and not entry.name.endswith(".part"):
                found.append(entry)
        except OSError:
            continue
    return found


def list_output_artifacts(
    thread_id: str,
    *,
    user_id: str | None = None,
    paths: Paths | None = None,
) -> list[str]:
    """Return sorted virtual paths for every deliverable in outputs.

    The scan is intentionally provider-agnostic: local and host-mounted
    sandboxes write directly into this directory, and remote providers download
    their outputs here before release. A missing directory is simply empty.
    """

    resolved = paths if paths is not None else get_paths()
    root = resolved.sandbox_outputs_dir(thread_id, user_id=user_id)
    if not root.is_dir():
        return []
    files = _walk_files(root)
    artifacts = [
        f"{OUTPUTS_VIRTUAL}/{path.relative_to(root).as_posix()}"
        for path in files
    ]
    artifacts.sort()
    return artifacts[:MAX_OUTPUT_ARTIFACTS]
