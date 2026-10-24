"""Knowledge HTTP routes: ownership, uploads, status and error mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from agentkit.knowledge.config import KnowledgeBaseConfig
from agentkit.knowledge.errors import KnowledgeDocumentNotFound
from agentkit.knowledge.models import (
    KnowledgeBase,
    KnowledgeDocument,
    SearchHit,
    SearchOutcome,
)
from agentkit.knowledge.service import status_from_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import knowledge


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class FakeService:
    def __init__(self) -> None:
        self.base = KnowledgeBase(
            id="b1", user_id="default", name="产品文档", type="technical",
            description=None, document_count=1, chunk_count=3,
            created_at=_now(), updated_at=_now(),
        )
        self.document = KnowledgeDocument(
            id="d1", user_id="default", knowledge_base_id="b1", filename="manual.md",
            stored_name="d1.md", mime_type="text/markdown", size_bytes=4,
            status="ready", chunk_count=3, error_message=None,
            created_at=_now(), updated_at=_now(),
        )
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        self.search_calls: list[dict] = []

    def status(self):
        return status_from_config(KnowledgeBaseConfig())

    async def list_bases(self, user_id: str):
        return [self.base]

    async def create_base(self, **kwargs):
        assert kwargs["user_id"] == "default"
        return self.base

    async def get_base(self, user_id: str, base_id: str):
        if base_id != "b1":
            from agentkit.knowledge.errors import KnowledgeBaseNotFound

            raise KnowledgeBaseNotFound("Knowledge base not found")
        return self.base

    async def update_base(self, **kwargs):
        return self.base

    async def delete_base(self, **kwargs):
        return False

    async def list_documents(self, user_id: str, base_id: str):
        return [self.document]

    async def get_document(self, user_id: str, document_id: str):
        if document_id != "d1":
            raise KnowledgeDocumentNotFound("Document not found")
        return self.document

    async def add_documents(self, **kwargs):
        self.uploaded = [item.filename for item in kwargs["uploads"]]
        return [self.document]

    async def reindex_document(self, **kwargs):
        return self.document

    async def delete_document(self, **kwargs):
        self.deleted.append(kwargs["document_id"])
        return kwargs["document_id"]

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return SearchOutcome(
            query=kwargs["query"],
            hits=[SearchHit(
                chunk_id="d1:0", document_id="d1", filename="manual.md", section="安装",
                content="安装步骤", score=0.9, knowledge_base_id="b1",
                knowledge_base_name="产品文档", knowledge_base_type="technical",
                rrf_score=0.0328, rerank_score=0.9,
            )],
            keyword_query=kwargs.get("keyword_query") or kwargs["query"],
            retrieval_mode="hybrid",
            reranked=True,
        )


def _client(service: FakeService | None = None) -> TestClient:
    app = FastAPI(redirect_slashes=False)
    install_middleware(app, auth_settings=AuthSettings.disabled())
    install_error_handlers(app)
    app.include_router(knowledge.router, prefix="/api")
    app.state.knowledge_service = service
    app.state.knowledge_config = KnowledgeBaseConfig()
    return TestClient(app, raise_server_exceptions=False)


def test_status_works_even_when_disabled() -> None:
    response = _client(None).get("/api/knowledge/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["embedding_dimensions"] == 4096
    assert {item["value"] for item in response.json()["types"]} == {
        "technical", "operations", "faq", "security", "api", "legal"
    }


def test_base_crud_and_upload_use_authenticated_default_user() -> None:
    service = FakeService()
    client = _client(service)
    assert client.get("/api/knowledge/bases").status_code == 200
    created = client.post(
        "/api/knowledge/bases",
        json={"name": "产品文档", "type": "technical"},
    )
    assert created.status_code == 201
    assert created.json()["chunk_count"] == 3

    bad_type = client.post(
        "/api/knowledge/bases", json={"name": "x", "type": "other"}
    )
    assert bad_type.status_code == 422

    uploaded = client.post(
        "/api/knowledge/bases/b1/documents",
        files=[("files", ("manual.md", b"text", "text/markdown"))],
    )
    assert uploaded.status_code == 202
    assert uploaded.json()["documents"][0]["id"] == "d1"
    assert service.uploaded == ["manual.md"]


def test_document_routes_check_base_ownership_shape() -> None:
    service = FakeService()
    client = _client(service)
    missing = client.get("/api/knowledge/bases/b2/documents/d1")
    assert missing.status_code == 404
    mismatch = client.get("/api/knowledge/bases/b2/documents/d1")
    assert mismatch.status_code == 404
    deleted = client.delete("/api/knowledge/bases/b1/documents/d1")
    assert deleted.status_code == 200
    assert service.deleted == ["d1"]


def test_search_forwards_type_and_base_id() -> None:
    service = FakeService()
    client = _client(service)
    response = client.get(
        "/api/knowledge/search",
        params={
            "q": "怎么安装",
            "keyword": "E1024 安装",
            "type": "technical",
            "base_id": "b1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["knowledge_base_type"] == "technical"
    assert body["results"][0]["rrf_score"] == 0.0328
    assert body["results"][0]["rerank_score"] == 0.9
    assert body["keyword_query"] == "E1024 安装"
    assert body["retrieval_mode"] == "hybrid"
    assert body["reranked"] is True
    assert service.search_calls[0]["knowledge_base_type"] == "technical"
    assert service.search_calls[0]["base_ids"] == ["b1"]
    assert service.search_calls[0]["keyword_query"] == "E1024 安装"


def test_search_without_keyword_lets_the_service_default_it() -> None:
    service = FakeService()
    client = _client(service)
    response = client.get("/api/knowledge/search", params={"q": "怎么安装"})
    assert response.status_code == 200
    assert service.search_calls[0]["keyword_query"] is None


def test_unknown_base_delete_is_404() -> None:
    service = FakeService()
    client = _client(service)
    response = client.delete("/api/knowledge/bases/b1")
    assert response.status_code == 404
    assert response.json()["code"] == "knowledge_base_not_found"
