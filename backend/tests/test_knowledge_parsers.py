"""MinerU local/cloud transports and the text passthrough parser."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
from agentkit.knowledge.config import CloudMinerUConfig, LocalMinerUConfig
from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.parsers.mineru import MineruCloudParser, MineruLocalParser
from agentkit.knowledge.parsers.text import LocalTextParser


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_local_mineru_posts_file_and_reads_full_md(tmp_path) -> None:
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-1.4")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=_zip({"full.md": "# 标题\n\n正文".encode()}), headers={"content-type": "application/zip"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruLocalParser(
        LocalMinerUConfig(endpoint="http://mineru:8000", api_key="secret"), client=client
    )
    parsed = await parser.parse(path, filename="manual.pdf")
    await parser.aclose()
    await client.aclose()

    assert parsed.markdown.startswith("# 标题")
    assert seen["url"] == "http://mineru:8000/v1/file_parse?response_format=zip&tier=flash&ocr_mode=txt"
    assert seen["auth"] == "Bearer secret"


@pytest.mark.asyncio
async def test_local_mineru_json_response_and_failure(tmp_path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"x")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"markdown": "# JSON 结果"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruLocalParser(LocalMinerUConfig(endpoint="http://mineru:8000", response_format="json"), client=client)
    parsed = await parser.parse(path, filename="x.pdf")
    assert "JSON 结果" in parsed.markdown
    await parser.aclose()
    await client.aclose()

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider details")

    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    bad = MineruLocalParser(LocalMinerUConfig(endpoint="http://mineru:8000"), client=bad_client)
    with pytest.raises(KnowledgeUnavailable) as excinfo:
        await bad.parse(path, filename="x.pdf")
    assert "provider details" not in str(excinfo.value)
    await bad.aclose()
    await bad_client.aclose()


@pytest.mark.asyncio
async def test_cloud_mineru_full_upload_poll_download_cycle(tmp_path) -> None:
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"pdf-bytes")
    calls: list[str] = []

    zip_payload = _zip({"full.md": "# 云端\n\n内容".encode()})

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v4/file-urls/batch":
            payload = json.loads(request.content)
            assert payload["files"][0]["name"] == "manual.pdf"
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "b1", "file_urls": ["http://upload.local/f1"]}})
        if request.url.host == "upload.local":
            assert request.content == b"pdf-bytes"
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/b1":
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "http://download.local/r.zip"}]}})
        if request.url.host == "download.local":
            return httpx.Response(200, content=zip_payload)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruCloudParser(
        CloudMinerUConfig(base_url="http://cloud.test", api_key="cloud-key", poll_interval_seconds=0.001),
        client=client,
    )
    parsed = await parser.parse(path, filename="manual.pdf")
    await parser.aclose()
    await client.aclose()

    assert "云端" in parsed.markdown
    assert calls[0] == "POST /api/v4/file-urls/batch"
    assert "PUT /f1" in calls
    assert "GET /api/v4/extract-results/batch/b1" in calls
    assert "GET /r.zip" in calls


@pytest.mark.asyncio
async def test_cloud_mineru_paths_come_from_config(tmp_path) -> None:
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"pdf-bytes")
    calls: list[str] = []
    zip_payload = _zip({"full.md": "# 代理\n\n走网关".encode()})

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/gateway/mineru/file-urls":
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "b9", "file_urls": ["http://upload.local/f9"]}})
        if request.url.host == "upload.local":
            return httpx.Response(200)
        if request.url.path == "/gateway/mineru/task/b9/result":
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "http://download.local/r9.zip"}]}})
        if request.url.host == "download.local":
            return httpx.Response(200, content=zip_payload)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruCloudParser(
        CloudMinerUConfig(
            base_url="http://cloud.test/",
            api_key="cloud-key",
            poll_interval_seconds=0.001,
            file_urls_path="gateway/mineru/file-urls",
            extract_results_path="/gateway/mineru/task/{batch_id}/result",
        ),
        client=client,
    )
    parsed = await parser.parse(path, filename="manual.pdf")
    await parser.aclose()
    await client.aclose()

    assert "代理" in parsed.markdown
    assert calls[0] == "POST /gateway/mineru/file-urls"
    assert "GET /gateway/mineru/task/b9/result" in calls


@pytest.mark.asyncio
async def test_cloud_mineru_failure_is_sanitized(tmp_path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"x")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad cloud token")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruCloudParser(
        CloudMinerUConfig(base_url="http://cloud.test", api_key="bad"), client=client
    )
    with pytest.raises(KnowledgeUnavailable) as excinfo:
        await parser.parse(path, filename="x.pdf")
    assert "bad cloud token" not in str(excinfo.value)
    await parser.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_cloud_mineru_parse_failure_includes_provider_error(tmp_path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"pdf-bytes")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"batch_id": "b-fail", "file_urls": ["http://upload.local/u"]}},
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "extract_result": [
                        {
                            "state": "failed",
                            "err_msg": "The file format is unsupported or OCR is required",
                        }
                    ]
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruCloudParser(
        CloudMinerUConfig(base_url="http://cloud.test", api_key="ok", poll_interval_seconds=0.01),
        client=client,
    )
    with pytest.raises(KnowledgeUnavailable, match="OCR is required") as excinfo:
        await parser.parse(path, filename="scan.pdf")
    assert "MinerU cloud parsing failed" in str(excinfo.value)
    await parser.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_cloud_mineru_api_error_includes_provider_message(tmp_path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"x")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "A0202", "msg": "insufficient quota"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = MineruCloudParser(
        CloudMinerUConfig(base_url="http://cloud.test", api_key="ok"), client=client
    )
    with pytest.raises(KnowledgeUnavailable, match="insufficient quota") as excinfo:
        await parser.parse(path, filename="x.pdf")
    assert "code A0202" in str(excinfo.value)
    await parser.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_text_parser_decodes_and_keeps_markdown(tmp_path) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes("﻿# 标题\n\n正文".encode())
    parser = LocalTextParser()
    parsed = await parser.parse(path, filename="notes.md")
    assert parsed.markdown.startswith("# 标题")
    assert parsed.metadata["encoding"] == "utf-8-sig"


@pytest.mark.asyncio
async def test_text_parser_rejects_binary(tmp_path) -> None:
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe\x00\x00bad")
    parser = LocalTextParser()
    with pytest.raises(KnowledgeUnavailable, match="decode"):
        await parser.parse(path, filename="bad.txt")
