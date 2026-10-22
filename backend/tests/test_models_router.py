"""M1 model catalog: whitelist fields, first-name wins, 404 and 422."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest
from agentkit.config import AppConfig, ModelConfig
from pydantic import ValidationError
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
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
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(models.router, prefix="/api")
    application.dependency_overrides.clear()
    client = TestClient(application, raise_server_exceptions=False)
    return client


def test_empty_catalog_is_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig())

    response = _client([]).get("/api/models")

    assert response.status_code == 200
    assert response.json() == {"models": [], "default_model": None}


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
    # 前端下拉需要的推理等级与默认模型也来自配置
    assert body["default_model"] == "flash"
    assert body["models"][0]["reasoning_efforts"] == []
    assert body["models"][0]["default_reasoning_effort"] is None
    text = response.text
    assert "sk-secret-value" not in text
    assert "when_thinking_enabled" not in text
    assert "api.example.test" not in text
    assert "api_key" not in text


def test_reasoning_efforts_come_from_config(monkeypatch) -> None:
    catalog = [
        _model(
            "flash",
            reasoning_efforts=[
                {"id": "low", "label": "低", "description": "快速响应"},
                {"id": "high", "label": "高", "default": True},
            ],
        ),
    ]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))

    body = _client(catalog).get("/api/models").json()

    model = body["models"][0]
    assert model["supports_reasoning_effort"] is True
    assert [item["id"] for item in model["reasoning_efforts"]] == ["low", "high"]
    assert model["reasoning_efforts"][0]["label"] == "低"
    assert model["reasoning_efforts"][1]["default"] is True
    assert model["default_reasoning_effort"] == "high"


def test_duplicate_or_double_default_efforts_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _model("flash", reasoning_efforts=["low", "low"])
    with pytest.raises(ValidationError):
        _model(
            "flash",
            reasoning_efforts=[
                {"id": "low", "default": True},
                {"id": "high", "default": True},
            ],
        )


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
