"""厂商模型目录：/models 为准、config 兜底、名字映射与缓存。"""

from __future__ import annotations

import httpx
import pytest
from agentkit.config import AppConfig, ModelConfig
from agentkit.models import catalog as catalog_mod
from agentkit.models.catalog import (
    build_model_catalog,
    provider_label,
    reasoning_efforts_for,
    split_model_name,
    vendor_model_name,
)


def _config(*models: ModelConfig, default_model: str | None = None) -> AppConfig:
    return AppConfig(models=list(models), agents={"default_model": default_model})


def _model(name: str, **overrides) -> ModelConfig:
    fields = {
        "name": name,
        "use": "langchain_openai:ChatOpenAI",
        "model": "gpt-4o-mini",
        "api_key": "sk-test",
        "base_url": "https://api.vendor.test/v1",
    }
    fields.update(overrides)
    return ModelConfig.model_validate(fields)


def _client(ids: list[str] | None, *, status: int = 200, calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if ids is None:
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(status, json={"data": [{"id": item} for item in ids]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_model_name_helpers_round_trip() -> None:
    assert split_model_name("flash") == ("flash", None)
    assert split_model_name("flash@deepseek-reasoner") == ("flash", "deepseek-reasoner")
    assert vendor_model_name("flash", "deepseek-reasoner") == "flash@deepseek-reasoner"


def test_provider_label_prefers_config_then_derives_from_use() -> None:
    assert provider_label(_model("a", provider="我的网关")) == "我的网关"
    assert provider_label(_model("b")) == "OpenAI"
    assert provider_label(_model("c", use="langchain_deepseek:ChatDeepSeek")) == "DeepSeek"
    assert provider_label(_model("d", use="some_vendor.client:ChatX")) == "Some Vendor"


def test_config_efforts_win_over_the_vendor_profile() -> None:
    cfg = _model("flash", reasoning_efforts=[{"id": "low", "label": "低"}])
    options = reasoning_efforts_for(cfg, _config(cfg))
    assert [option.id for option in options] == ["low"]
    assert options[0].display == "低"


def test_reasoning_efforts_fall_back_to_the_vendor_profile(monkeypatch) -> None:
    pytest.importorskip("langchain_openai")
    cfg = _model("gpt5", model="gpt-5")
    options = reasoning_efforts_for(cfg, _config(cfg))
    # 厂商 profile 只给 id：展示名原样用 id（不改大小写），只有 none 叫 Off
    assert [option.id for option in options][:2] == ["minimal", "low"]
    assert options[0].display == "minimal"


def test_reasoning_efforts_are_empty_without_any_source() -> None:
    cfg = _model("plain", model="gpt-4o-mini")
    assert reasoning_efforts_for(cfg, _config(cfg)) == []


@pytest.mark.asyncio
async def test_vendor_ids_drive_the_list_and_keep_the_config_row_first() -> None:
    cfg = _config(_model("flash"), _model("openai-mini", display_name="Mini"))
    client = _client(["gpt-4o-mini", "gpt-4o", "o3"])
    result = await build_model_catalog(cfg, client=client, use_cache=False)
    await client.aclose()

    assert [item.name for item in result.models] == [
        "flash",
        "flash@gpt-4o",
        "flash@o3",
        "openai-mini",
        "openai-mini@gpt-4o",
        "openai-mini@o3",
    ]
    # config 条目那一行保留自己的展示名；厂商新报的行用 vendor id 当展示名
    assert result.models[0].display_name is None
    assert result.models[1].display_name == "gpt-4o"
    assert result.models[3].display_name == "Mini"
    assert {item.provider for item in result.models} == {"OpenAI"}
    assert result.vendor_errors == []


@pytest.mark.asyncio
async def test_vendor_failure_falls_back_to_config_and_reports_why() -> None:
    cfg = _config(_model("flash"))
    client = _client(None, status=503)
    result = await build_model_catalog(cfg, client=client, use_cache=False)
    await client.aclose()

    assert [item.name for item in result.models] == ["flash"]
    assert result.models[0].source == "config"
    assert result.vendor_errors == ["OpenAI: RuntimeError"]


@pytest.mark.asyncio
async def test_catalog_is_cached_between_calls() -> None:
    cfg = _config(_model("flash"))
    calls: list[str] = []
    client = _client(["gpt-4o-mini"], calls=calls)
    catalog_mod.clear_catalog_cache()

    await build_model_catalog(cfg, client=client)
    await build_model_catalog(cfg, client=client)
    await client.aclose()

    assert len(calls) == 1
    catalog_mod.clear_catalog_cache()


@pytest.mark.asyncio
async def test_default_effort_comes_from_the_default_model() -> None:
    cfg = _config(
        _model("flash", reasoning_efforts=[{"id": "high", "default": True}]),
        _model("pro", reasoning_efforts=["low"]),
        default_model="pro",
    )
    client = _client(["gpt-4o-mini"])
    result = await build_model_catalog(cfg, client=client, use_cache=False)
    await client.aclose()

    assert result.default_model == "pro"
    assert result.default_effort is None  # pro 没有标 default，就是"不指定"


@pytest.mark.asyncio
async def test_vendor_ids_go_through_the_documented_name_pattern() -> None:
    cfg = _config(_model("flash"))
    client = _client(["gpt-4o-mini", "gpt-4o"])
    result = await build_model_catalog(cfg, client=client, use_cache=False)
    await client.aclose()

    names = [item.name for item in result.models]
    assert all(len(name) <= 128 for name in names)
    assert all(" " not in name for name in names)
