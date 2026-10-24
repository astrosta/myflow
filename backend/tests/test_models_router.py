"""M1 模型目录：厂商 /models 为准，config 兜底；白名单字段、404、422。"""

from __future__ import annotations

import httpx
import pytest
from agentkit.config import AppConfig, ModelConfig
from agentkit.models import catalog as catalog_mod
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import models
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


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


def _patch_vendor(monkeypatch, ids: list[str] | None = None, *, status: int = 200) -> None:
    """把厂商目录换成 mock transport：ids=None 表示厂商挂掉（500）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if ids is None:
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(status, json={"data": [{"id": item} for item in ids]})

    monkeypatch.setattr(
        catalog_mod,
        "make_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    catalog_mod.clear_catalog_cache()


def _client() -> TestClient:
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(models.router, prefix="/api")
    application.dependency_overrides.clear()
    return TestClient(application, raise_server_exceptions=False)


def test_empty_catalog_is_an_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig())
    _patch_vendor(monkeypatch, [])

    response = _client().get("/api/models")

    assert response.status_code == 200
    assert response.json() == {
        "models": [],
        "default_model": None,
        "default_reasoning_effort": None,
        "vendor_errors": [],
    }


def test_vendor_list_drives_the_catalog_and_new_ids_get_qualified_names(monkeypatch) -> None:
    catalog = [_model("flash", display_name="Flash", description="public")]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))
    _patch_vendor(monkeypatch, ["deepseek-v4-flash", "deepseek-reasoner"])

    body = _client().get("/api/models").json()

    # 厂商报出来的 id 全部可选：配置里有的沿用 name，其余是 `<name>@<vendor-id>`
    assert [item["name"] for item in body["models"]] == [
        "flash",
        "flash@deepseek-reasoner",
    ]
    assert [item["model"] for item in body["models"]] == [
        "deepseek-v4-flash",
        "deepseek-reasoner",
    ]
    assert {item["source"] for item in body["models"]} == {"vendor"}
    assert body["models"][0]["provider"] == "OpenAI"
    assert body["models"][0]["display_name"] == "Flash"
    assert body["models"][1]["display_name"] == "deepseek-reasoner"
    assert body["default_model"] == "flash"


def test_vendor_failure_falls_back_to_config(monkeypatch) -> None:
    catalog = [_model("flash"), _model("pro", context_window=128000)]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))
    _patch_vendor(monkeypatch, None)

    body = _client().get("/api/models").json()

    assert [item["name"] for item in body["models"]] == ["flash", "pro"]
    assert {item["source"] for item in body["models"]} == {"config"}
    assert body["vendor_errors"]
    assert body["models"][1]["context_window"] == 128000


def test_list_keeps_the_first_name_and_hides_secrets(monkeypatch) -> None:
    catalog = [
        _model("flash", display_name="First"),
        _model("flash", display_name="Second"),
        _model("pro", supports_vision=True, context_window=128000),
    ]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))
    _patch_vendor(monkeypatch, ["deepseek-v4-flash"])

    response = _client().get("/api/models")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["models"]] == ["flash", "pro"]
    assert body["models"][0]["display_name"] == "First"
    assert body["models"][1]["supports_vision"] is True
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
            provider="我的网关",
            reasoning_efforts=[
                {"id": "low", "label": "低", "description": "快速响应"},
                {"id": "high", "label": "高", "default": True},
            ],
        ),
    ]
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig(models=catalog))
    _patch_vendor(monkeypatch, ["deepseek-v4-flash"])

    item = _client().get("/api/models").json()["models"][0]

    assert item["supports_reasoning_effort"] is True
    assert item["provider"] == "我的网关"
    assert [effort["id"] for effort in item["reasoning_efforts"]] == ["low", "high"]
    assert item["reasoning_efforts"][0]["label"] == "低"
    assert item["reasoning_efforts"][1]["default"] is True
    assert item["default_reasoning_effort"] == "high"


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
    client = _client()

    found = client.get("/api/models/flash")
    missing = client.get("/api/models/missing")

    assert found.status_code == 200
    assert found.json()["display_name"] == "First"
    assert "sk-secret-value" not in found.text
    assert missing.status_code == 404
    assert missing.json()["code"] == "model_not_found"


def test_illegal_model_name_is_422(monkeypatch) -> None:
    monkeypatch.setattr(models, "get_app_config", lambda: AppConfig())
    _patch_vendor(monkeypatch, [])
    client = _client()

    spaced = client.get("/api/models/bad name")
    too_long = client.get("/api/models/" + ("a" * 129))

    assert spaced.status_code == 422
    assert spaced.json()["code"] == "invalid_request"
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "invalid_request"
