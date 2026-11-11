"""Upload and artifact files. Routes pass virtual paths; this module talks to the disk."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import UploadFile
from fastapi.responses import FileResponse

from agentkit.config.paths import get_paths
from agentkit.persistence.base import iso_utc
from agentkit.sandbox.artifacts import OUTPUTS_VIRTUAL, list_output_artifacts
from agentkit.sandbox.security import PathMapping, _REJECT_HINT, find_path_mapping, resolve_sandbox_path
from backend.app.gateway.errors import (
    ArtifactNotFound,
    ArtifactPathInvalid,
    UploadInvalid,
    UploadLimitReached,
    UploadNotFound,
    UploadTooLarge,
)
from backend.app.gateway.models.response import UploadedFileInfo

_UPLOADS_VIRTUAL = "/mnt/user-data/uploads"
_OUTPUTS_VIRTUAL = OUTPUTS_VIRTUAL
_USER_DATA = "/mnt/user-data"
_ATTACHMENT_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}
_MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".md": "text/markdown",
}


@dataclass(frozen=True, slots=True)
class UploadLimits:
    max_files_per_request: int = 10
    max_files_per_thread: int = 100
    max_file_size_bytes: int = 10 * 1024 * 1024
    max_total_size_bytes: int = 32 * 1024 * 1024


def upload_limits() -> UploadLimits:
    from agentkit.config import get_app_config

    try:
        raw = getattr(get_app_config(), "uploads", None)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return UploadLimits()
    defaults = UploadLimits()
    return UploadLimits(
        max_files_per_request=int(raw.get("max_files_per_request", defaults.max_files_per_request)),
        max_files_per_thread=int(raw.get("max_files_per_thread", defaults.max_files_per_thread)),
        max_file_size_bytes=int(raw.get("max_file_size_bytes", defaults.max_file_size_bytes)),
        max_total_size_bytes=int(raw.get("max_total_size_bytes", defaults.max_total_size_bytes)),
    )


def check_upload_filename(filename: str) -> str:
    """Reject traversal and separators. The error text does not echo the name."""
    if (
        not filename
        or len(filename) > 255
        or "\x00" in filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or any(part in {".", ".."} for part in filename.replace("\\", "/").split("/"))
    ):
        raise UploadInvalid("Invalid upload filename")
    return filename


def _mappings(thread_id: str, user_id: str | None) -> list[PathMapping]:
    root = get_paths().sandbox_user_data_dir(thread_id, user_id=user_id)
    return [
        PathMapping(_USER_DATA, str(root)),
        PathMapping(f"{_USER_DATA}/workspace", str(root / "workspace")),
        PathMapping(_UPLOADS_VIRTUAL, str(root / "uploads")),
        PathMapping(_OUTPUTS_VIRTUAL, str(root / "outputs")),
    ]


def _uploads_dir(thread_id: str, user_id: str | None) -> Path:
    return resolve_sandbox_path(_UPLOADS_VIRTUAL, _mappings(thread_id, user_id))


def _stored_name(directory: Path, original: str) -> str:
    if not (directory / original).exists():
        return original
    stem, dot, ext = original.rpartition(".")
    base = stem if dot and stem else original
    suffix = f".{ext}" if dot and stem else ""
    number = 2
    while True:
        candidate = f"{base}_{number}{suffix}"
        if not (directory / candidate).exists():
            return candidate
        number += 1


def _info(directory: Path, filename: str, original: str) -> UploadedFileInfo:
    path = directory / filename
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return UploadedFileInfo(
        filename=filename,
        original_filename=original,
        size=path.stat().st_size,
        virtual_path=f"{_UPLOADS_VIRTUAL}/{filename}",
        modified=iso_utc(modified) or "",
    )


def _list_files(directory: Path) -> list[UploadedFileInfo]:
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        found.append(_info(directory, path.name, path.name))
    return found


async def list_upload_dir(thread_id: str, user_id: str | None) -> list[UploadedFileInfo]:
    directory = _uploads_dir(thread_id, user_id)
    return await asyncio.to_thread(_list_files, directory)


async def list_artifacts(thread_id: str, user_id: str | None) -> list[str]:
    """List deliverables under outputs without requiring the caller to know names."""

    return await asyncio.to_thread(
        list_output_artifacts, thread_id, user_id=user_id, paths=get_paths()
    )


def _write_upload(directory: Path, original: str, chunks: list[bytes], limits: UploadLimits, used: int) -> tuple[Path, int]:
    size = 0
    part = directory / f".upload-{uuid4().hex}.part"
    try:
        with part.open("wb") as handle:
            for chunk in chunks:
                size += len(chunk)
                if size > limits.max_file_size_bytes or used + size > limits.max_total_size_bytes:
                    raise UploadTooLarge("Upload exceeds the size limit")
                handle.write(chunk)
        if size > limits.max_file_size_bytes:
            raise UploadTooLarge("Upload exceeds the size limit")
        filename = _stored_name(directory, original)
        target = directory / filename
        part.replace(target)
        return target, size
    except Exception:
        part.unlink(missing_ok=True)
        raise


async def save_uploads(thread_id: str, user_id: str | None, files: list[UploadFile]) -> list[UploadedFileInfo]:
    limits = upload_limits()
    if len(files) > limits.max_files_per_request:
        raise UploadInvalid("Too many files in one request")
    names = [check_upload_filename(item.filename or "") for item in files]
    directory = _uploads_dir(thread_id, user_id)
    await asyncio.to_thread(get_paths().ensure_thread_dirs, thread_id, user_id=user_id)
    existing = await asyncio.to_thread(_list_files, directory)
    if len(existing) + len(files) > limits.max_files_per_thread:
        raise UploadLimitReached("This thread has reached the upload limit")

    saved: list[Path] = []
    infos: list[UploadedFileInfo] = []
    used = 0
    try:
        for upload, original in zip(files, names, strict=True):
            chunks: list[bytes] = []
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(part) for part in chunks) > limits.max_file_size_bytes:
                    raise UploadTooLarge("Upload exceeds the size limit")
                if used + sum(len(part) for part in chunks) > limits.max_total_size_bytes:
                    raise UploadTooLarge("Upload exceeds the size limit")
            target, size = await asyncio.to_thread(_write_upload, directory, original, chunks, limits, used)
            saved.append(target)
            used += size
            infos.append(_info(directory, target.name, original))
    except Exception:
        for path in saved:
            path.unlink(missing_ok=True)
        raise
    return infos


async def delete_upload_file(thread_id: str, user_id: str | None, filename: str) -> None:
    directory = _uploads_dir(thread_id, user_id)
    target = resolve_sandbox_path(f"{_UPLOADS_VIRTUAL}/{filename}", _mappings(thread_id, user_id))

    def _remove() -> None:
        if not target.is_file() or target.parent != directory:
            raise UploadNotFound(f"Upload {filename} not found")
        target.unlink()

    await asyncio.to_thread(_remove)


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _disposition(filename: str, media_type: str, download: bool) -> str:
    attachment = download or media_type in _ATTACHMENT_TYPES
    kind = "attachment" if attachment else "inline"
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class OpenedArtifact:
    def __init__(self, response: FileResponse) -> None:
        self.response = response


async def open_artifact(thread_id: str, user_id: str | None, path: str, *, download: bool) -> OpenedArtifact:
    virtual = "/" + path.lstrip("/")
    mappings = _mappings(thread_id, user_id)
    mapping = find_path_mapping(virtual, mappings)
    outputs = _OUTPUTS_VIRTUAL
    if mapping is None or mapping.container_path.rstrip("/") != outputs:
        raise ArtifactPathInvalid(_REJECT_HINT)
    try:
        host = resolve_sandbox_path(virtual, mappings)
    except PermissionError as exc:
        raise ArtifactPathInvalid(str(exc)) from exc

    def _check() -> bool:
        return host.is_file()

    if not await asyncio.to_thread(_check):
        raise ArtifactNotFound("Artifact not found")
    media_type = _media_type(host)
    return OpenedArtifact(
        FileResponse(
            host,
            media_type=media_type,
            headers={"Content-Disposition": _disposition(host.name, media_type, download)},
        )
    )
