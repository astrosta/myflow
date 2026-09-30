"""Shared glob and grep for local filesystem roots. Returns virtual paths."""

from __future__ import annotations

import re
from pathlib import Path

from agentkit.sandbox.sandbox import GrepMatch


def _virtualize(host: Path, root: Path, virtual_root: str) -> str:
    relative = host.resolve().relative_to(root.resolve()).as_posix()
    if not relative or relative == ".":
        return virtual_root
    return f"{virtual_root.rstrip('/')}/{relative}"


def glob_search(
    root: Path,
    virtual_root: str,
    pattern: str,
    *,
    include_dirs: bool = False,
    max_results: int = 200,
) -> tuple[list[str], bool]:
    matches: list[str] = []
    truncated = False
    if not root.exists():
        raise FileNotFoundError(root)
    try:
        for path in root.rglob(pattern):
            if path.is_dir() and not include_dirs:
                continue
            if not path.is_file() and not (include_dirs and path.is_dir()):
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            matches.append(_virtualize(path, root, virtual_root))
    except OSError:
        raise
    return matches, truncated


def grep_search(
    root: Path,
    virtual_root: str,
    pattern: str,
    *,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> tuple[list[GrepMatch], bool]:
    if not root.exists():
        raise FileNotFoundError(root)
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    matches: list[GrepMatch] = []
    truncated = False
    files = [root] if root.is_file() else list(root.rglob(glob or "*"))
    for path in files:
        if not path.is_file():
            continue
        if glob and root.is_dir() and not path.match(glob):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            raise OSError(f"Failed to read {path.name}") from err
        virtual = _virtualize(path, root if root.is_dir() else root.parent, virtual_root if root.is_dir() else virtual_root.rsplit("/", 1)[0])
        if root.is_file():
            virtual = virtual_root
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line) is None:
                continue
            if len(matches) >= max_results:
                truncated = True
                return matches, truncated
            matches.append(GrepMatch(path=virtual, line_number=number, line=line))
    return matches, truncated
