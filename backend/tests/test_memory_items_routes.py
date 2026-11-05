"""HTTP contract for the memory management page."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.agents.memory.openviking.library import (
    InvalidMemoryURI,
    MemoryContent,
    MemoryDetail,
    MemoryItem,
    MemoryItemNotFound,
    MemoryListPage,
    MemoryUnavailable,
    MemoryWriteConflict,
    TocEntry,
)
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.deps import get_memory_library
from backend.app.gateway.errors import MemoryRuntimeUnavailable
from backend.app.gateway.routers import memory_items

URI = "viking://user/u1/memories/preferences/alice/style.md"
FOLDER = "viking://user/u1/memories/preferences/alice"


class FakeLibrary:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def list_memory_items(self, user_id, memory_type, *, limit):
        self.calls.append(("list", user_id, memory_type, limit))
        if self.error:
            raise self.error
        return MemoryListPage(
            items=(
                MemoryItem(
                    uri=URI,
                    name="style.md",
                    folder=FOLDER,
                    memory_type="preferences",
                    abstract="喜欢简洁",
                    abstract_truncated=False,
                    updated_at="2026-11-02T00:00:00Z",
                ),
            ),
            has_more=True,
            limit=limit,
        )

    async def memory_detail(self, user_id, uri):
        self.calls.append(("detail", user_id, uri))
        if self.error:
            raise self.error
        return MemoryDetail(
            uri=uri,
            folder=FOLDER,
            memory_type="preferences",
            name="style.md",
            overview="# Prefs",
            toc=(
                TocEntry("heading", "Prefs", 1),
                TocEntry("link", "Style", 2, URI),
            ),
        )

    async def read_memory_content(self, user_id, uri):
        self.calls.append(("read", user_id, uri))
        if self.error:
            raise self.error
        return MemoryContent(uri=uri, content="hello", sha256="a" * 64, size_bytes=5)

    async def update_memory_content(self, user_id, uri, content, *, expected_sha256):
        self.calls.append(("update", user_id, uri, content, expected_sha256))
        if self.error:
            raise self.error
        return MemoryContent(uri=uri, content=content, sha256="b" * 64, size_bytes=len(content))

    async def delete_memory_item(self, user_id, uri):
        self.calls.append(("delete", user_id, uri))
        if self.error:
            raise self.error


def _client(library: FakeLibrary | None = None, *, unavailable: bool = False) -> TestClient:
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(memory_items.router, prefix="/api")
    if unavailable:

        def _raise() -> FakeLibrary:
            raise MemoryRuntimeUnavailable("long-term memory is not enabled")

        app.dependency_overrides[get_memory_library] = _raise
    else:
        fake = library or FakeLibrary()
        app.dependency_overrides[get_memory_library] = lambda: fake
    return TestClient(app, raise_server_exceptions=False)


def test_types_endpoint_lists_manageable_types_without_skills() -> None:
    client = _client()

    body = client.get("/api/memory/items/types").json()

    assert [(item["type"], item["label"]) for item in body["items"]] == [
        ("preferences", "偏好"),
        ("entities", "实体"),
        ("events", "事件"),
        ("cases", "案例"),
        ("trajectories", "轨迹"),
        ("experiences", "经验"),
        ("tools", "工具"),
    ]
    assert all(item["type"] != "skills" for item in body["items"])


def test_list_maps_summaries_and_forwards_type_and_limit() -> None:
    fake = FakeLibrary()
    client = _client(fake)

    response = client.get("/api/memory/items?type=preferences&limit=20")

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "preferences"
    assert body["has_more"] is True
    assert body["limit"] == 20
    assert body["items"][0] == {
        "uri": URI,
        "name": "style.md",
        "folder": FOLDER,
        "memory_type": "preferences",
        "abstract": "喜欢简洁",
        "abstract_truncated": False,
        "updated_at": "2026-11-02T00:00:00Z",
    }
    assert fake.calls[0] == ("list", "default", "preferences", 20)


def test_unknown_type_is_422_without_touching_the_library() -> None:
    fake = FakeLibrary()
    client = _client(fake)

    response = client.get("/api/memory/items?type=skills")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_memory_uri"
    assert fake.calls == []


def test_detail_returns_toc() -> None:
    client = _client()

    body = client.get("/api/memory/items/detail", params={"uri": URI}).json()

    assert body["folder"] == FOLDER
    assert body["toc"] == [
        {"kind": "heading", "title": "Prefs", "depth": 1, "uri": None, "navigable": False},
        {"kind": "link", "title": "Style", "depth": 2, "uri": URI, "navigable": True},
    ]
    assert body["overview_missing"] is False


def test_content_read_update_and_delete() -> None:
    fake = FakeLibrary()
    client = _client(fake)

    content = client.get("/api/memory/items/content", params={"uri": URI})
    assert content.status_code == 200
    assert content.json()["sha256"] == "a" * 64

    saved = client.put(
        "/api/memory/items/content",
        json={"uri": URI, "content": "new body", "expected_sha256": "a" * 64},
    )
    assert saved.status_code == 200
    assert saved.json()["sha256"] == "b" * 64
    assert fake.calls[-1][-1] == "a" * 64

    deleted = client.request("DELETE", "/api/memory/items", params={"uri": URI})
    assert deleted.status_code == 204
    assert fake.calls[-1] == ("delete", "default", URI)


def test_errors_are_mapped_to_coded_responses() -> None:
    fake = FakeLibrary()
    client = _client(fake)

    fake.error = InvalidMemoryURI("memory sidecars are read-only")
    invalid = client.request("DELETE", "/api/memory/items", params={"uri": URI})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_memory_uri"

    fake.error = MemoryWriteConflict("a" * 64)
    conflict = client.put(
        "/api/memory/items/content",
        json={"uri": URI, "content": "x", "expected_sha256": "a" * 64},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "memory_conflict"

    fake.error = MemoryItemNotFound("gone")
    missing = client.request("DELETE", "/api/memory/items", params={"uri": URI})
    assert missing.status_code == 404
    assert missing.json()["code"] == "memory_not_found"

    fake.error = MemoryUnavailable("down")
    unavailable = client.get("/api/memory/items")
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "memory_runtime_unavailable"


def test_disabled_runtime_is_503() -> None:
    client = _client(unavailable=True)

    assert client.get("/api/memory/items/types").status_code == 503
    response = client.get("/api/memory/items")

    assert response.status_code == 503
    assert response.json()["code"] == "memory_runtime_unavailable"


def test_update_rejects_unknown_fields() -> None:
    client = _client()

    response = client.put(
        "/api/memory/items/content",
        json={"uri": URI, "content": "x", "unexpected": True},
    )

    assert response.status_code == 422
