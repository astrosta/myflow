"""Upload routes: names, limits, listing, and thread visibility."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config.paths import Paths
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.persistence.run.memory import MemoryRunStore
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import threads, uploads
from backend.app.gateway.thread_files import UploadLimits


def _services() -> SimpleNamespace:
    return SimpleNamespace(
        run_manager=SimpleNamespace(),
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=MemoryThreadMetaStore(), checkpointer=SimpleNamespace()),
        run_store=MemoryRunStore(),
        event_store=SimpleNamespace(),
    )


def _client(services: SimpleNamespace, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr("backend.app.gateway.thread_files.get_paths", lambda: Paths(tmp_path))
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(threads.router, prefix="/api")
    application.include_router(uploads.router, prefix="/api")
    application.state.run_services = services
    return TestClient(application, raise_server_exceptions=False)


def _thread(client: TestClient, thread_id: str = "thread-1") -> None:
    assert client.post("/api/threads", json={"thread_id": thread_id}).status_code == 201


def test_empty_upload_is_422(tmp_path, monkeypatch) -> None:
    client = _client(_services(), tmp_path, monkeypatch)
    _thread(client)

    response = client.post("/api/threads/thread-1/uploads")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_upload"


def test_bad_filenames_do_not_echo_the_path(tmp_path, monkeypatch) -> None:
    client = _client(_services(), tmp_path, monkeypatch)
    _thread(client)

    for name in ("../x", "a/b.txt"):
        response = client.post(
            "/api/threads/thread-1/uploads",
            files=[("files", (name, b"hello", "text/plain"))],
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_upload"
        assert name not in response.text
        assert str(tmp_path) not in response.text


def test_oversize_and_thread_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.gateway.thread_files.upload_limits",
        lambda: UploadLimits(max_file_size_bytes=4, max_total_size_bytes=4, max_files_per_thread=1),
    )
    client = _client(_services(), tmp_path, monkeypatch)
    _thread(client)

    too_big = client.post(
        "/api/threads/thread-1/uploads",
        files=[("files", ("big.txt", b"12345", "text/plain"))],
    )
    assert too_big.status_code == 413
    assert too_big.json()["code"] == "upload_too_large"

    assert (
        client.post(
            "/api/threads/thread-1/uploads",
            files=[("files", ("one.txt", b"ok", "text/plain"))],
        ).status_code
        == 201
    )
    limited = client.post(
        "/api/threads/thread-1/uploads",
        files=[("files", ("two.txt", b"ok", "text/plain"))],
    )
    assert limited.status_code == 409
    assert limited.json()["code"] == "upload_limit_reached"


def test_duplicate_name_is_renamed_and_list_is_not_a_filename(tmp_path, monkeypatch) -> None:
    client = _client(_services(), tmp_path, monkeypatch)
    _thread(client)
    first = client.post(
        "/api/threads/thread-1/uploads",
        files=[("files", ("report.pdf", b"one", "application/pdf"))],
    )
    second = client.post(
        "/api/threads/thread-1/uploads",
        files=[("files", ("report.pdf", b"two", "application/pdf"))],
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["files"][0]["filename"] == "report_2.pdf"
    assert second.json()["files"][0]["virtual_path"] == "/mnt/user-data/uploads/report_2.pdf"
    assert str(tmp_path) not in second.text

    listed = client.get("/api/threads/thread-1/uploads/list")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert {item["filename"] for item in listed.json()["files"]} == {"report.pdf", "report_2.pdf"}

    missing = client.get("/api/threads/other/uploads/list")
    assert missing.status_code == 404
    assert missing.json()["code"] == "thread_not_found"
