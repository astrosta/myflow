"""File helpers for E2BSandbox.

Text and bytes both go through the E2B file API. E2B's
``Filesystem.read`` supports ``format="text" | "bytes" | "stream"``, so
binary reads use the native bytes mode instead of a base64 shell transfer.

Search (``glob``/``grep``) runs one small Python helper dropped into the
sandbox at ``SEARCH_HELPER_PATH``. The SDK has no glob and no content search, and
pulling the tree to the Gateway to filter it would cross the wire twice; the
helper caps results and rejects escaping patterns before it serializes anything.

Every helper raises the ``Sandbox`` contract's errors: a missing or unreadable
file is an ``OSError``, a credential failure is a ``SandboxRuntimeError``, and a
path-guard rejection stays a ``PermissionError``.
"""

from __future__ import annotations

import base64
import json
import posixpath
import shlex
import weakref
from typing import Any

from agentkit.sandbox.e2b.client import (
    SandboxGoneError,
    SdkSandbox,
    error_status,
    is_gone_error,
    is_missing_path_error,
    is_unreachable_error,
)
from agentkit.sandbox.exceptions import SandboxError, SandboxRuntimeError
from agentkit.sandbox.sandbox import GrepMatch

# A whole file crosses the wire in one E2B request now, so this is only a cap:
# an artifact larger than this is refused instead of buffered in the Gateway.
_MAX_TRANSFER_BYTES = 256 * 1024 * 1024
_MKDIR_TIMEOUT_SECONDS = 30.0
_READ_RANGE_TIMEOUT_SECONDS = 60.0
_LARGE_FILE_THRESHOLD = 1024 * 1024
_MAX_READ_CHARS = 100_000
_TRUNCATED_NOTE = "\n\n[truncated: file exceeds the read limit; narrow the line range]"
SEARCH_HELPER_PATH = "/tmp/.myflow/search.py"
# Searching walks a tree inside the sandbox, so it gets a wall clock of its own
# instead of the 600s command budget.
_SEARCH_TIMEOUT_SECONDS = 120.0
# Handles whose helper is already installed. Keyed by handle, not by
# E2BSandbox: two sandboxes over one handle must not upload it twice.
_PREPARED_SEARCH_HELPERS: weakref.WeakSet[Any] = weakref.WeakSet()
_AUTH_STATUS_CODES = frozenset({401, 403})
_TIMEOUT_TYPE_NAMES = frozenset(
    {
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "Timeout",
        "TimeoutError",
        "APITimeoutError",
    }
)


def read_text(
    sdk: SdkSandbox,
    path: str,
    *,
    user: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = _MAX_READ_CHARS,
) -> str:
    """Read text, or a 1-indexed inclusive line range of it."""

    if start_line is None and end_line is None:
        return _truncate(_read_all_text(sdk, path, user=user), max_chars)
    if _is_large_file(sdk, path, user=user):
        return _read_line_range_with_sed(
            sdk,
            path,
            user=user,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        )
    return _slice_lines(
        _read_all_text(sdk, path, user=user),
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
    )


def write_text(
    sdk: SdkSandbox,
    path: str,
    content: str,
    *,
    user: str,
    append: bool = False,
) -> None:
    """Create the parent directory, then write text (optionally appending)."""

    ensure_parent_dir(sdk, path, user=user)
    payload = content
    if append:
        payload = _read_existing_text(sdk, path, user=user) + content
    _write_payload(sdk, path, payload, user=user)


def read_bytes(
    sdk: SdkSandbox,
    path: str,
    *,
    user: str,
    max_bytes: int = _MAX_TRANSFER_BYTES,
) -> bytes:
    """Read bytes through the E2B native bytes mode.

    One request carries the whole file, so the size is checked before the
    download: an oversized artifact must fail rather than be buffered in the
    Gateway. A directory is rejected here too, by the same probe.
    """

    size = _stat_size(sdk, path, user=user)
    if size is not None and size > max_bytes:
        raise OSError(
            f"Failed to read {path}: it exceeds the {max_bytes}-byte transfer limit"
        )
    try:
        data = sdk.files.read(path, format="bytes", user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action="read") from exc
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise OSError(f"Failed to read {path}: the file API returned non-bytes data")
    if len(data) > max_bytes:
        raise OSError(
            f"Failed to read {path}: it exceeds the {max_bytes}-byte transfer limit"
        )
    return data


def write_bytes(sdk: SdkSandbox, path: str, content: bytes, *, user: str) -> None:
    """Create the parent directory, then write bytes through the file API."""

    ensure_parent_dir(sdk, path, user=user)
    _write_payload(sdk, path, bytes(content), user=user)


def ensure_parent_dir(sdk: SdkSandbox, path: str, *, user: str) -> None:
    """``mkdir -p`` the parent of ``path`` inside the sandbox.

    The file API has no recursive create, and a write into a missing directory
    would fail. The root mount's parent (``/``) is skipped.
    """

    parent = posixpath.dirname(path.rstrip("/"))
    if not parent or parent == "/":
        return
    command = f"mkdir -p -- {shlex.quote(parent)}"
    _run_command(
        sdk,
        command,
        user=user,
        timeout=_MKDIR_TIMEOUT_SECONDS,
        path=path,
        action="create the parent directory of",
    )


def list_tree(
    sdk: SdkSandbox, path: str, *, user: str, max_depth: int = 2
) -> list[str]:
    """Render ``path`` as tree lines, with the glyphs ``render_tree`` emits.

    Listing goes through the file API one directory at a time, so a tree costs
    one round trip per listed directory and stops at ``max_depth``. Empty
    (``[]``) and failure stay distinguishable for the tool layer: a missing path
    is ``FileNotFoundError`` and a file path is ``NotADirectoryError``.
    """
    lines: list[str] = []

    def walk(directory: str, prefix: str, depth: int) -> None:
        entries = _list_directory_entries(sdk, directory, user=user)
        for index, (name, is_dir) in enumerate(entries):
            last = index == len(entries) - 1
            branch = "└── " if last else "├── "
            label = f"{name}/" if is_dir else name
            lines.append(f"{prefix}{branch}{label}")
            if not is_dir or depth + 1 >= max_depth:
                continue
            extension = "    " if last else "│   "
            walk(posixpath.join(directory, name), prefix + extension, depth + 1)

    walk(path.rstrip("/") or "/", "", 0)
    return lines


def ensure_search_helper(sdk: SdkSandbox, *, user: str) -> None:
    """Install ``SEARCH_HELPER`` at ``SEARCH_HELPER_PATH`` once per sandbox handle.

    The file API does not create directories, so the parent gets a ``mkdir -p``
    first. The helper lives outside the mounts on purpose: it is infrastructure,
    not user data, so the mount path guard never covers it. The handle carries
    the "already installed" bit: another sandbox over the same handle must not
    upload the helper again.
    """
    if _search_helper_ready(sdk):
        return
    ensure_parent_dir(sdk, SEARCH_HELPER_PATH, user=user)
    try:
        sdk.files.write(SEARCH_HELPER_PATH, SEARCH_HELPER, user=user)
    except Exception as exc:
        raise _map_file_error(
            exc, SEARCH_HELPER_PATH, action="install the search helper at"
        ) from exc
    _mark_search_helper_ready(sdk)


def glob_paths(
    sdk: SdkSandbox,
    path: str,
    pattern: str,
    *,
    user: str,
    include_dirs: bool = False,
    max_results: int = 200,
) -> tuple[list[str], bool]:
    """Match ``pattern`` under ``path`` inside the sandbox.

    ``**`` crosses directories, exactly like the local provider's ``rglob``.
    Returns ``(matches, truncated)``; a missing root is ``FileNotFoundError``,
    like ``render_tree``.
    """
    payload: dict[str, Any] = {
        "mode": "glob",
        "path": path,
        "pattern": pattern,
        "include_dirs": bool(include_dirs),
        "max_results": int(max_results),
    }
    matches, truncated = _run_search_helper(sdk, payload, user=user, path=path)
    return _glob_matches(matches, path), truncated


def grep_paths(
    sdk: SdkSandbox,
    path: str,
    pattern: str,
    *,
    user: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> tuple[list[GrepMatch], bool]:
    """Search file contents under ``path`` inside the sandbox.

    ``literal``, ``case_sensitive`` and the optional ``glob`` filter are resolved by
    the helper's ``re``, which is the engine the local provider uses too.
    Returns ``(matches, truncated)``.
    """
    payload: dict[str, Any] = {
        "mode": "grep",
        "path": path,
        "pattern": pattern,
        "glob": glob,
        "literal": bool(literal),
        "case_sensitive": bool(case_sensitive),
        "max_results": int(max_results),
    }
    matches, truncated = _run_search_helper(sdk, payload, user=user, path=path)
    return [_to_grep_match(item, path) for item in matches], truncated


def command_stdout(result: Any) -> str:
    """The SDK command result's stdout as text."""

    return _as_text(getattr(result, "stdout", ""))


def command_exit_code(result: Any) -> int:
    """The SDK command result's exit code, with a non-zero fallback for None."""

    code = getattr(result, "exit_code", 0)
    if code is None:
        return -1
    try:
        return int(code)
    except (TypeError, ValueError):
        return -1


def is_timeout_error(exc: BaseException) -> bool:
    """Whether an SDK/HTTP exception reports a timeout of the request itself.

    E2B models "the sandbox is gone" and "the sandbox cannot be reached" as
    subclasses of its TimeoutException. Those are lifecycle failures, so they
    must not be reported as a command or transfer deadline.
    """

    if is_gone_error(exc) or is_unreachable_error(exc):
        return False
    return any(cls.__name__ in _TIMEOUT_TYPE_NAMES for cls in type(exc).__mro__)


def _read_all_text(sdk: SdkSandbox, path: str, *, user: str) -> str:
    try:
        data = sdk.files.read(path, user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action="read") from exc
    return _as_text(data)


def _read_existing_text(sdk: SdkSandbox, path: str, *, user: str) -> str:
    """Current text for an append, or an empty string when the file is new."""

    try:
        exists = bool(sdk.files.exists(path, user=user))
    except Exception as exc:
        raise _map_file_error(exc, path, action="read") from exc
    if not exists:
        return ""
    return _read_all_text(sdk, path, user=user)


def _write_payload(
    sdk: SdkSandbox, path: str, payload: str | bytes, *, user: str
) -> None:
    try:
        sdk.files.write(path, payload, user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action="write") from exc


def _stat_size(sdk: SdkSandbox, path: str, *, user: str) -> int | None:
    try:
        entry = sdk.files.stat(path, user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action="read") from exc
    if not isinstance(entry, dict):
        return None
    if _is_directory_entry(entry):
        raise OSError(f"Failed to read {path}: is a directory")
    raw = entry.get("size")
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return None


def _is_large_file(sdk: SdkSandbox, path: str, *, user: str) -> bool:
    """Best-effort size probe; a failed probe just means "read it normally"."""

    try:
        entry = sdk.files.stat(path, user=user)
        size = int(entry.get("size"))
    except Exception:
        return False
    return size > _LARGE_FILE_THRESHOLD


def _read_line_range_with_sed(
    sdk: SdkSandbox,
    path: str,
    *,
    user: str,
    start_line: int | None,
    end_line: int | None,
    max_chars: int,
) -> str:
    """Slice a large file inside the sandbox so it never crosses the wire."""

    start = max(start_line or 1, 1)
    if end_line is not None and end_line < start:
        return ""
    end = "$" if end_line is None else str(end_line)
    limit = max_chars + 1
    command = (
        f"sed -n {shlex.quote(f'{start},{end}p')} -- {shlex.quote(path)}"
        f" | head -c {limit}"
    )
    result = _run_command(
        sdk,
        command,
        user=user,
        timeout=_READ_RANGE_TIMEOUT_SECONDS,
        path=path,
        action="read",
    )
    text = command_stdout(result)
    if len(text) > max_chars:
        return text[:max_chars] + _TRUNCATED_NOTE
    return text[:-1] if text.endswith("\n") else text


def _run_command(
    sdk: SdkSandbox,
    command: str,
    *,
    user: str,
    timeout: float,
    path: str,
    action: str,
) -> Any:
    try:
        result = sdk.commands.run(command, timeout=timeout, user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action=action) from exc
    code = command_exit_code(result)
    if code != 0:
        raise OSError(f"Failed to {action} {path}: command exited with code {code}")
    return result


def _slice_lines(
    text: str,
    *,
    start_line: int | None,
    end_line: int | None,
    max_chars: int,
) -> str:
    lines = text.splitlines()
    start = max((start_line or 1) - 1, 0)
    end = end_line if end_line is not None else len(lines)
    return _truncate("\n".join(lines[start:end]), max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATED_NOTE


def _list_directory_entries(
    sdk: SdkSandbox, directory: str, *, user: str
) -> list[tuple[str, bool]]:
    try:
        entries = sdk.files.list(directory, user=user)
    except NotADirectoryError as exc:
        raise NotADirectoryError(f"{directory}: not a directory") from exc
    except Exception as exc:
        raise _map_list_error(sdk, exc, directory, user=user) from exc
    return _sorted_entries(entries, path=directory)


def _sorted_entries(entries: Any, *, path: str) -> list[tuple[str, bool]]:
    """Directories first, then case-insensitive names, like ``render_tree``."""

    result: list[tuple[str, bool]] = []
    for entry in entries or ():
        name = _entry_name(entry, path=path)
        if name is None:
            continue
        result.append((name, _is_directory_entry(entry)))
    return sorted(result, key=lambda item: (not item[1], item[0].lower()))


def _entry_name(entry: Any, *, path: str) -> str | None:
    """One entry's name; ``.``/``..`` rows are skipped, junk raises.

    Dropping an entry we cannot name would turn a broken listing into an empty
    directory, and the tool layer renders that as "(empty)". A nameless entry is
    a listing failure instead.
    """
    name = entry.get("name") if isinstance(entry, dict) else None
    if isinstance(name, str):
        name = name.strip("/")
        if name in (".", ".."):
            return None
        if name and "/" not in name:
            return name
    raise OSError(
        f"Failed to list {path}: the file API returned an entry without a usable name"
    )


def _map_list_error(
    sdk: SdkSandbox, exc: BaseException, path: str, *, user: str
) -> Exception:
    """Translate a failed listing, separating "missing" from "not a directory".

    SDK builds disagree about the error a file path produces, so anything that
    is not already a missing/auth/timeout case gets one ``stat`` probe before it
    is reported as a transport failure.
    """
    if isinstance(exc, PermissionError):
        return exc
    status = error_status(exc)
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            "E2BSandbox rejected the file request; check sandbox.e2b.api_key"
        )
    if is_missing_path_error(exc):
        return FileNotFoundError(f"{path}: no such file or directory")
    if is_gone_error(exc):
        return SandboxGoneError(f"Failed to list {path}: the sandbox is no longer running")
    if is_timeout_error(exc):
        return OSError(f"Failed to list {path}: the sandbox file API timed out")
    entry = _probe_stat(sdk, path, user=user)
    if entry is not None and not _is_directory_entry(entry):
        return NotADirectoryError(f"{path}: not a directory")
    return OSError(f"Failed to list {path}: {exc}")


def _probe_stat(sdk: SdkSandbox, path: str, *, user: str) -> dict[str, Any] | None:
    try:
        entry = sdk.files.stat(path, user=user)
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _run_search_helper(
    sdk: SdkSandbox, payload: dict[str, Any], *, user: str, path: str
) -> tuple[list[Any], bool]:
    """Run one helper invocation and decode its JSON answer."""

    ensure_search_helper(sdk, user=user)
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    # base64 keeps the payload inert: quoting and globbing cannot reach it.
    command = f"python3 {shlex.quote(SEARCH_HELPER_PATH)} {shlex.quote(encoded)}"
    try:
        result = sdk.commands.run(command, timeout=_SEARCH_TIMEOUT_SECONDS, user=user)
    except Exception as exc:
        raise _map_file_error(exc, path, action="search") from exc
    return _parse_search_output(result, path)


def _parse_search_output(result: Any, path: str) -> tuple[list[Any], bool]:
    payload = _json_object(command_stdout(result))
    if payload is None:
        code = command_exit_code(result)
        detail = _as_text(getattr(result, "stderr", "")).strip()
        note = f": {detail}" if detail else ""
        raise OSError(
            f"Failed to search {path}: the search helper printed no JSON "
            f"(exit code {code}){note}"
        )
    error = payload.get("error")
    if error:
        message = f"Failed to search {path}: {error}"
        if payload.get("missing") is True:
            raise FileNotFoundError(message)
        raise OSError(message)
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise OSError(f"Failed to search {path}: the helper returned no match list")
    return matches, bool(payload.get("truncated", False))


def _glob_matches(matches: list[Any], path: str) -> list[str]:
    result: list[str] = []
    for item in matches:
        if not isinstance(item, str):
            raise OSError(
                f"Failed to search {path}: the helper returned a malformed match"
            )
        result.append(item)
    return result


def _to_grep_match(item: Any, path: str) -> GrepMatch:
    if not isinstance(item, dict):
        raise OSError(
            f"Failed to search {path}: the helper returned a malformed match"
        )
    match_path = item.get("path")
    line = item.get("line")
    if not isinstance(match_path, str) or not isinstance(line, str):
        raise OSError(
            f"Failed to search {path}: the helper returned a malformed match"
        )
    try:
        number = int(item.get("line_number"))
    except (TypeError, ValueError) as exc:
        raise OSError(
            f"Failed to search {path}: the helper returned a malformed line number"
        ) from exc
    return GrepMatch(path=match_path, line_number=number, line=line)


def _json_object(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _search_helper_ready(sdk: SdkSandbox) -> bool:
    try:
        return sdk in _PREPARED_SEARCH_HELPERS
    except TypeError:  # unhashable, or a handle that cannot be weakly referenced
        return False


def _mark_search_helper_ready(sdk: SdkSandbox) -> None:
    try:
        _PREPARED_SEARCH_HELPERS.add(sdk)
    except TypeError:
        return


def _is_directory_entry(entry: dict[str, Any]) -> bool:
    return "DIR" in str(entry.get("type") or "").upper()


def _map_file_error(exc: BaseException, path: str, *, action: str) -> Exception:
    """Translate an SDK/transport failure into the ``Sandbox`` error family."""

    if isinstance(exc, (SandboxError, PermissionError)):
        return exc
    status = error_status(exc)
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            "E2BSandbox rejected the file request; check sandbox.e2b.api_key"
        )
    # A path request reads a bare not-found as a missing path; E2B names the
    # sandbox-gone cases explicitly, so those are still reported as gone.
    if is_missing_path_error(exc):
        return OSError(f"Failed to {action} {path}: no such file or directory")
    if is_gone_error(exc):
        return SandboxGoneError(f"Failed to {action} {path}: the sandbox is no longer running")
    if is_timeout_error(exc):
        return OSError(f"Failed to {action} {path}: the sandbox file API timed out")
    if is_unreachable_error(exc):
        return OSError(f"Failed to {action} {path}: the sandbox could not be reached: {exc}")
    return OSError(f"Failed to {action} {path}: {exc}")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# The helper is the only code this module ships into the sandbox. It stays in
# one string so installing it costs a single write and the Gateway never has to
# carry the tree. Keep it dependency-free and Python 3.10 compatible: the image
# is the user's template, not this project's venv.
SEARCH_HELPER = r'''#!/usr/bin/env python3
"""Bounded glob/grep for one directory inside the sandbox.

argv[1] is base64-encoded JSON:

    {"mode": "glob", "path": "<absolute>", "pattern": "**/*.py",
     "include_dirs": false, "max_results": 200}
    {"mode": "grep", "path": "<absolute>", "pattern": "needle", "glob": null,
     "literal": false, "case_sensitive": false, "max_results": 100}

Exactly one JSON object is written to stdout:

    {"matches": [...], "truncated": false}
    {"error": "<why>", "missing": false}

Patterns (and grep's glob filter) must be relative and must not climb out of the
search root. Results are capped before anything is serialized, so a wide tree
cannot push a large payload back to the Gateway.
"""

import base64
import json
import re
import sys
from pathlib import Path, PurePosixPath

MAX_LINE_CHARS = 400
MAX_RESULTS = 1000
DEFAULT_RESULTS = 100


def emit(payload):
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    raise SystemExit(0)


def fail(message, missing=False):
    emit({"error": str(message), "missing": missing})


def read_payload():
    if len(sys.argv) < 2:
        fail("missing search payload")
    try:
        decoded = base64.b64decode(sys.argv[1].encode("ascii"), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:
        fail("invalid search payload")
    if not isinstance(payload, dict):
        fail("search payload must be a JSON object")
    return payload


def text_field(payload, key):
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        fail("%s must be a string" % key)
    return value


def search_root(payload):
    base = text_field(payload, "path")
    if not base:
        fail("path must not be blank")
    if ".." in Path(base).parts:
        fail("path must not escape the search root")
    root = Path(base)
    if not root.is_absolute():
        fail("path must be absolute")
    if not root.exists():
        fail("%s: no such file or directory" % base, missing=True)
    return root


def relative_pattern(payload, key):
    pattern = text_field(payload, key)
    if not pattern.strip():
        fail("%s must not be blank" % key)
    pure = PurePosixPath(pattern)
    if pure.is_absolute():
        fail("%s must be relative to the search root" % key)
    if ".." in pure.parts:
        fail("%s must not escape the search root" % key)
    return pattern


def result_limit(payload):
    try:
        limit = int(payload.get("max_results", DEFAULT_RESULTS))
    except (TypeError, ValueError):
        limit = DEFAULT_RESULTS
    return max(1, min(limit, MAX_RESULTS))


def run_glob(payload):
    root = search_root(payload)
    pattern = relative_pattern(payload, "pattern")
    include_dirs = bool(payload.get("include_dirs", False))
    limit = result_limit(payload)
    if not root.is_dir():
        fail("%s: not a directory" % root)
    matches = []
    truncated = False
    for candidate in sorted(root.rglob(pattern)):
        if candidate.is_dir():
            if not include_dirs:
                continue
        elif not candidate.is_file():
            continue
        if len(matches) >= limit:
            truncated = True
            break
        matches.append(str(candidate))
    emit({"matches": matches, "truncated": truncated})


def run_grep(payload):
    root = search_root(payload)
    expression = text_field(payload, "pattern")
    if not expression:
        fail("pattern must not be blank")
    filter_value = text_field(payload, "glob")
    filter_pattern = relative_pattern(payload, "glob") if filter_value else "*"
    literal = bool(payload.get("literal", False))
    case_sensitive = bool(payload.get("case_sensitive", False))
    limit = result_limit(payload)
    try:
        regex = re.compile(
            re.escape(expression) if literal else expression,
            0 if case_sensitive else re.IGNORECASE,
        )
    except re.error as exc:
        fail("invalid pattern: %s" % exc)
    files = [root] if root.is_file() else sorted(root.rglob(filter_pattern))
    matches = []
    truncated = False
    for path in files:
        if not path.is_file():
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            fail("%s: %s" % (path, exc))
        with handle:
            for number, line in enumerate(handle, start=1):
                if regex.search(line) is None:
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": str(path),
                        "line_number": number,
                        "line": line.rstrip("\r\n")[:MAX_LINE_CHARS],
                    }
                )
        if truncated:
            break
    emit({"matches": matches, "truncated": truncated})


def main():
    payload = read_payload()
    mode = text_field(payload, "mode")
    if mode == "glob":
        run_glob(payload)
    elif mode == "grep":
        run_grep(payload)
    else:
        fail("mode must be glob or grep")


main()
'''


__all__ = [
    "SEARCH_HELPER",
    "SEARCH_HELPER_PATH",
    "command_exit_code",
    "command_stdout",
    "ensure_parent_dir",
    "ensure_search_helper",
    "glob_paths",
    "grep_paths",
    "is_timeout_error",
    "list_tree",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
]
