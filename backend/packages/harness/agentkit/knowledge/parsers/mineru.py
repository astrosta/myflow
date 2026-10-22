"""MinerU latest HTTP APIs: local V1 file_parse and mineru.net cloud batch."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from agentkit.knowledge.config import CloudMinerUConfig, LocalMinerUConfig, ParserConfig
from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.models import ParsedDocument
from agentkit.knowledge.parsers.text import LocalTextParser

logger = logging.getLogger(__name__)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _extract_markdown_from_json(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload if payload.strip() else None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_markdown_from_json(item)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("markdown", "md", "full_md", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (dict, list)):
            found = _extract_markdown_from_json(value)
            if found:
                return found
    for key in ("data", "result", "results", "files"):
        if key in payload:
            found = _extract_markdown_from_json(payload[key])
            if found:
                return found
    return None


def _content_list_to_markdown(payload: Any) -> str | None:
    if not isinstance(payload, list):
        return None
    lines: list[str] = []
    for block in payload:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str) and text.strip():
            block_type = str(block.get("type") or "")
            if block_type in {"title", "heading"}:
                level = int(block.get("text_level") or 1)
                lines.append("#" * max(1, min(level, 6)) + " " + text.strip())
            else:
                lines.append(text.strip())
    return "\n\n".join(lines) if lines else None


def extract_zip_markdown(data: bytes, *, max_bytes: int) -> ParsedDocument:
    if len(data) > max_bytes:
        raise KnowledgeUnavailable("MinerU result exceeds the configured download limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise KnowledgeUnavailable("MinerU returned an invalid archive") from exc
    names = archive.namelist()
    markdown = ""
    chosen = ""
    for name in sorted(names, key=lambda item: (not item.endswith("full.md"), len(item))):
        if name.endswith("full.md"):
            payload = archive.read(name)
            if len(payload) > max_bytes:
                raise KnowledgeUnavailable("MinerU markdown exceeds the configured download limit")
            markdown = payload.decode("utf-8", errors="replace")
            chosen = name
            break
    if not markdown:
        for name in names:
            if name.endswith("content_list.json"):
                try:
                    payload = json.loads(archive.read(name).decode("utf-8", errors="replace"))
                except (ValueError, UnicodeDecodeError):
                    continue
                converted = _content_list_to_markdown(payload)
                if converted:
                    markdown = converted
                    chosen = name
                    break
    if not markdown:
        for name in names:
            if name.endswith(".md"):
                markdown = archive.read(name).decode("utf-8", errors="replace")
                chosen = name
                break
    if not markdown.strip():
        raise KnowledgeUnavailable("MinerU returned no markdown output")
    return ParsedDocument(markdown=markdown, metadata={"parser": "mineru", "artifact": chosen, "files": names})


def _parse_response(data: bytes, content_type: str, *, max_bytes: int) -> ParsedDocument:
    if data[:2] == b"PK" or "zip" in content_type:
        return extract_zip_markdown(data, max_bytes=max_bytes)
    if len(data) > max_bytes:
        raise KnowledgeUnavailable("MinerU result exceeds the configured download limit")
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise KnowledgeUnavailable("MinerU returned neither zip nor JSON") from exc
    markdown = _extract_markdown_from_json(payload)
    if not markdown:
        raise KnowledgeUnavailable("MinerU JSON response contains no markdown")
    return ParsedDocument(markdown=markdown, metadata={"parser": "mineru", "format": "json"})


class MineruLocalParser:
    def __init__(self, config: LocalMinerUConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    async def parse(self, path: Path, *, filename: str) -> ParsedDocument:
        url = self._config.endpoint.rstrip("/") + "/" + self._config.file_parse_path.lstrip("/")
        params = {
            "response_format": self._config.response_format,
            "tier": self._config.tier,
            "ocr_mode": self._config.ocr_mode,
        }
        payload = await asyncio.to_thread(path.read_bytes)
        try:
            response = await self._http().post(
                url,
                params=params,
                files={"files": (filename, payload, _mime(filename))},
                headers=_auth(self._config.api_key),
            )
        except httpx.HTTPError as exc:
            raise KnowledgeUnavailable(f"MinerU local service is unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise KnowledgeUnavailable(f"MinerU local parse failed (HTTP {response.status_code})")
        return _parse_response(
            response.content,
            response.headers.get("content-type", ""),
            max_bytes=self._config.max_download_bytes,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


class MineruCloudParser:
    def __init__(self, config: CloudMinerUConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_seconds, connect=30.0)
            )
        return self._client

    async def parse(self, path: Path, *, filename: str) -> ParsedDocument:
        http = self._http()
        headers = _auth(self._config.api_key)
        payload = {
            "enable_formula": self._config.enable_formula,
            "enable_table": self._config.enable_table,
            "language": self._config.language,
            "model_version": self._config.model_version,
            "files": [{"name": filename, "is_ocr": self._config.is_ocr}],
        }
        try:
            created = await http.post(
                self._config.base_url.rstrip("/") + "/api/v4/file-urls/batch",
                json=payload,
                headers=headers,
            )
            data = self._json(created, "create upload")
            batch_id = str(data["batch_id"])
            file_urls = data.get("file_urls") or []
            if not file_urls:
                raise KnowledgeUnavailable("MinerU cloud returned no upload URL")
            upload_url = str(file_urls[0])
            uploaded = await http.put(
                upload_url, content=await asyncio.to_thread(path.read_bytes)
            )
            if uploaded.status_code >= 400:
                raise KnowledgeUnavailable(f"MinerU cloud upload failed (HTTP {uploaded.status_code})")
            result = await self._poll(batch_id, headers)
            zip_url = result.get("full_zip_url")
            if not zip_url:
                raise KnowledgeUnavailable("MinerU cloud result has no full_zip_url")
            downloaded = await http.get(str(zip_url))
            if downloaded.status_code >= 400:
                raise KnowledgeUnavailable(f"MinerU cloud download failed (HTTP {downloaded.status_code})")
            return extract_zip_markdown(downloaded.content, max_bytes=self._config.max_download_bytes)
        except KnowledgeUnavailable:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise KnowledgeUnavailable(f"MinerU cloud request failed: {type(exc).__name__}") from exc

    async def _poll(self, batch_id: str, headers: dict[str, str]) -> dict[str, Any]:
        url = self._config.base_url.rstrip("/") + f"/api/v4/extract-results/batch/{batch_id}"
        deadline = time.monotonic() + self._config.timeout_seconds
        while time.monotonic() < deadline:
            response = await self._http().get(url, headers=headers)
            data = self._json(response, "poll result")
            rows = data.get("extract_result") or data.get("extract_results") or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                state = str(row.get("state") or row.get("status") or "").lower()
                if state in {"done", "success", "completed"}:
                    return row
                if state in {"failed", "error"}:
                    raise KnowledgeUnavailable("MinerU cloud parsing failed")
            await asyncio.sleep(self._config.poll_interval_seconds)
        raise KnowledgeUnavailable("MinerU cloud parsing timed out")

    @staticmethod
    def _json(response: httpx.Response, action: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise KnowledgeUnavailable(f"MinerU cloud {action} failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeUnavailable(f"MinerU cloud {action} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise KnowledgeUnavailable(f"MinerU cloud {action} returned an invalid body")
        code = payload.get("code")
        if code not in (None, 0, "0", 200):
            raise KnowledgeUnavailable(f"MinerU cloud {action} failed (code {code})")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KnowledgeUnavailable(f"MinerU cloud {action} returned no data")
        return data

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def build_parser(config: ParserConfig):
    if config.mode == "cloud":
        return MineruCloudParser(config.cloud)
    return MineruLocalParser(config.local)


__all__ = [
    "LocalTextParser",
    "MineruCloudParser",
    "MineruLocalParser",
    "build_parser",
    "extract_zip_markdown",
]
