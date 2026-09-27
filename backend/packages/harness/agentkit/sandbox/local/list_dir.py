"""Tree rendering for ``list_dir``. Empty directories produce no lines."""

from __future__ import annotations

from pathlib import Path


def render_tree(root: Path, max_depth: int = 2) -> list[str]:
    """Return tree lines. Missing path raises ``FileNotFoundError``; listing failure raises ``OSError``."""
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    resolved_root = root.resolve()
    lines: list[str] = []

    def walk(directory: Path, prefix: str, depth: int) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as err:
            raise OSError(f"Failed to list {directory.name}") from err
        for index, entry in enumerate(entries):
            last = index == len(entries) - 1
            branch = "└── " if last else "├── "
            name = f"{entry.name}/" if entry.is_dir() else entry.name
            lines.append(f"{prefix}{branch}{name}")
            if not entry.is_dir() or depth + 1 >= max_depth:
                continue
            try:
                child = entry.resolve()
            except OSError:
                continue
            if not child.is_relative_to(resolved_root):
                continue
            extension = "    " if last else "│   "
            walk(entry, prefix + extension, depth + 1)

    walk(root, "", 0)
    return lines
