"""M1 model catalog: whitelist fields, first-name wins, 404 and 422."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config import AppConfig, ModelConfig
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.routers import models


def _model(name: str, **overrides) -> ModelConfig:
    fields = dict(
        name=name,
        model="deepseek-v4-flash",
        use="langchain_openai.ChatOpenAI",
        api_key="sk-secret-value",
        base_url="https://api.example.test",
        when_thinking_enabled={"budget": 99},
        supports_thinking=True,
        display_name=name,
    )
    fields.update(overrides)
    return ModelConfig(**fields)


def _client(configs: list[ModelConfig]) -> TestClient:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application)
    install_error_handlers(application)
    application.include_router(models.router, prefix="/api")
    application.dependency_overrides.clear()
    client = TestClient(application, raise_server_exceptions=False)
    return client


def test_empty_catalog_is_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig())

    response = _client([]).get("/api/models")

    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_list_keeps_the_first_name_and_hides_secrets(monkeypatch) -> None:
    catalog = [
        _model("flash", display_name="First", description="public"),
        _model("flash", display_name="Second"),
        _model("pro", supports_vision=True, context_window=128000),
    ]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))

    response = _client(catalog).get("/api/models")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["models"]] == ["flash", "pro"]
    assert body["models"][0]["display_name"] == "First"
    assert body["models"][0]["supports_thinking"] is True
    assert body["models"][1]["context_window"] == 128000
    text = response.text
    assert "sk-secret-value" not in text
    assert "when_thinking_enabled" not in text
    assert "api.example.test" not in text
    assert "api_key" not in text


def test_get_model_returns_the_first_entry_or_404(monkeypatch) -> None:
    catalog = [
        _model("flash", display_name="First"),
        _model("flash", display_name="Second"),
    ]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))
    client = _client(catalog)

    found = client.get("/api/models/flash")
    missing = client.get("/api/models/missing")

    assert found.status_code == 200
    assert found.json()["display_name"] == "First"
    assert "sk-secret-value" not in found.text
    assert missing.status_code == 404
    assert missing.json()["code"] == "model_not_found"


def test_illegal_model_name_is_422(monkeypatch) -> None:
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig())
    client = _client([])

    spaced = client.get("/api/models/bad name")
    too_long = client.get("/api/models/" + ("a" * 129))

    assert spaced.status_code == 422
    assert spaced.json()["code"] == "invalid_request"
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "invalid_request"
