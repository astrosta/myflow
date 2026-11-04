"""File helpers for CubeSandbox.

Text goes through the SDK's envd file API. Real ``cubesandbox`` 0.7.3 exposes
only ``Filesystem.read(path, *, user=None) -> str`` -- there is no ``format``
argument and no bytes mode. Binary transfers therefore never ask the file API
for bytes: they use a bounded ``dd`` + ``base64 -w0`` pipeline over
``commands.run``, one 256 KiB slice at a time.

Every helper raises the ``Sandbox`` contract's errors: a missing or unreadable
file is an ``OSError``, a credential failure is a ``SandboxRuntimeError``, and a
path-guard rejection stays a ``PermissionError``.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import shlex
from typing import Any

from agentkit.sandbox.cube.client import SdkSandbox, error_status
from agentkit.sandbox.exceptions import SandboxError, SandboxRuntimeError

# One dd slice per round trip. Large enough that a few MiB costs a few calls,
# small enough that a single response cannot balloon the Gateway's memory.
_TRANSFER_CHUNK_SIZE = 256 * 1024
_TRANSFER_TIMEOUT_SECONDS = 120.0
_MAX_TRANSFER_BYTES = 256 * 1024 * 1024
_MKDIR_TIMEOUT_SECONDS = 30.0
_READ_RANGE_TIMEOUT_SECONDS = 60.0
_LARGE_FILE_THRESHOLD = 1024 * 1024
_MAX_READ_CHARS = 100_000
_TRUNCATED_NOTE = "\n\n[truncated: file exceeds the read limit; narrow the line range]"
_AUTH_STATUS_CODES = frozenset({401, 403})
_MISSING_STATUS_CODES = frozenset({404, 410})
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
    chunk_size: int = _TRANSFER_CHUNK_SIZE,
    timeout: float = _TRANSFER_TIMEOUT_SECONDS,
    max_bytes: int = _MAX_TRANSFER_BYTES,
) -> bytes:
    """Read bytes through bounded base64 slices.

    The size comes from the file API, so a missing file fails before any
    command runs. A file whose size cannot be reported falls back to reading
    until a slice comes back empty, capped by ``max_bytes``.
    """

    size = _stat_size(sdk, path, user=user)
    chunks: list[bytes] = []
    offset = 0
    index = 0
    while size is None or offset < size:
        if offset >= max_bytes:
            raise OSError(
                f"Failed to read {path}: it exceeds the {max_bytes}-byte transfer limit"
            )
        expected = chunk_size if size is None else min(chunk_size, size - offset)
        chunk = _read_chunk(
            sdk,
            path,
            user=user,
            index=index,
            chunk_size=chunk_size,
            timeout=timeout,
        )
        if not chunk:
            if size is None:
                break
            raise OSError(
                f"Failed to read {path}: the transfer stopped after "
                f"{offset} of {size} bytes"
            )
        if size is not None and len(chunk) != expected:
            if offset + len(chunk) == size:
                chunks.append(chunk)
                break
            raise OSError(
                f"Failed to read {path}: expected {expected} bytes but the "
                f"sandbox returned {len(chunk)}"
            )
        chunks.append(chunk)
        offset += len(chunk)
        index += 1
    return b"".join(chunks)


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
    """Whether an SDK/HTTP exception reports a timeout."""

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


def _read_chunk(
    sdk: SdkSandbox,
    path: str,
    *,
    user: str,
    index: int,
    chunk_size: int,
    timeout: float,
) -> bytes:
    command = (
        f"dd if={shlex.quote(path)} bs={chunk_size} skip={index} count=1"
        " status=none 2>/dev/null | base64 -w0"
    )
    result = _run_command(
        sdk, command, user=user, timeout=timeout, path=path, action="read"
    )
    return _decode_base64(command_stdout(result), path)


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


def _decode_base64(payload: str, path: str) -> bytes:
    try:
        return base64.b64decode(payload.strip())
    except (binascii.Error, ValueError) as exc:
        raise OSError(
            f"Failed to read {path}: the sandbox returned invalid base64"
        ) from exc


def _is_directory_entry(entry: dict[str, Any]) -> bool:
    return "DIR" in str(entry.get("type") or "").upper()


def _map_file_error(exc: BaseException, path: str, *, action: str) -> Exception:
    """Translate an SDK/transport failure into the ``Sandbox`` error family."""

    if isinstance(exc, (SandboxError, PermissionError)):
        return exc
    status = error_status(exc)
    if status in _AUTH_STATUS_CODES:
        return SandboxRuntimeError(
            "CubeSandbox rejected the file request; check sandbox.cube.api_key"
        )
    if is_timeout_error(exc):
        return OSError(f"Failed to {action} {path}: the sandbox file API timed out")
    if status in _MISSING_STATUS_CODES:
        return OSError(f"Failed to {action} {path}: no such file or directory")
    return OSError(f"Failed to {action} {path}: {exc}")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "command_exit_code",
    "command_stdout",
    "ensure_parent_dir",
    "is_timeout_error",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
]
