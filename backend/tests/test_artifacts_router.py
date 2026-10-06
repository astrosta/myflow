"""Artifact downloads stay inside outputs and do not execute HTML."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config.paths import Paths
from agentkit.persistence.thread_meta.memory import MemoryThreadMetaStore
from agentkit.runtime.runs.store.memory import MemoryRunStore
from agentkit.sandbox.security import _REJECT_HINT
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.routers import artifacts, threads


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr("backend.app.gateway.thread_files.get_paths", lambda: Paths(tmp_path))
    services = SimpleNamespace(
        run_manager=SimpleNamespace(),
        bridge=SimpleNamespace(supports_cross_process=False),
        run_context=SimpleNamespace(thread_store=MemoryThreadMetaStore(), checkpointer=SimpleNamespace()),
        run_store=MemoryRunStore(),
        event_store=SimpleNamespace(),
    )
    application = FastAPI(redirect_slashes=False)
    install_middleware(application)
    install_error_handlers(application)
    application.include_router(threads.router, prefix="/api")
    application.include_router(artifacts.router, prefix="/api")
    application.state.run_services = services
    return TestClient(application, raise_server_exceptions=False)


def test_outputs_file_is_served_and_html_is_an_attachment(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="default")
    outputs = paths.sandbox_outputs_dir("thread-1", user_id="default")
    (outputs / "note.txt").write_text("hello", encoding="utf-8")
    (outputs / "page.html").write_text("<p>hi</p>", encoding="utf-8")
    (outputs / "icon.svg").write_text("<svg/>", encoding="utf-8")

    note = client.get("/api/threads/thread-1/artifacts/mnt/user-data/outputs/note.txt")
    page = client.get("/api/threads/thread-1/artifacts/mnt/user-data/outputs/page.html")
    icon = client.get("/api/threads/thread-1/artifacts/mnt/user-data/outputs/icon.svg")

    assert note.status_code == 200
    assert note.text == "hello"
    assert "inline" in note.headers["content-disposition"]
    assert "attachment" in page.headers["content-disposition"]
    assert "attachment" in icon.headers["content-disposition"]


def test_non_output_paths_and_missing_files(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/threads", json={"thread_id": "thread-1"}).status_code == 201
    Paths(tmp_path).ensure_thread_dirs("thread-1", user_id="default")

    uploads = client.get("/api/threads/thread-1/artifacts/mnt/user-data/uploads/secret.txt")
    workspace = client.get("/api/threads/thread-1/artifacts/mnt/user-data/workspace/secret.txt")
    escaped = client.get("/api/threads/thread-1/artifacts/mnt/user-data/outputs/../../etc/passwd")
    missing = client.get("/api/threads/thread-1/artifacts/mnt/user-data/outputs/missing.txt")

    assert uploads.status_code == 422
    assert uploads.json()["code"] == "invalid_artifact_path"
    assert uploads.json()["detail"] == _REJECT_HINT
    assert workspace.status_code == 422
    assert workspace.json()["code"] == "invalid_artifact_path"
    assert escaped.status_code == 422
    assert escaped.json()["detail"] == _REJECT_HINT
    assert "passwd" not in escaped.json()["detail"]
    assert missing.status_code == 404
    assert missing.json()["code"] == "artifact_not_found"
