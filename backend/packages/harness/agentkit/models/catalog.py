"""Vendor model catalog: ask the provider what it serves, then map it back to config.

两条来源：

1. **厂商 API**（`GET {base_url}/models`）给出这个 key 真实可用的模型 id——列表以它为准；
2. **config.yaml 的 `models[]`** 给出每个模型怎么建（use/base_url/api_key/展示名），
   以及可选的 `reasoning_efforts` 文案覆盖。

选中的模型标识：config 里正好有这个 id → 用 `models[].name`；厂商新冒出来的 id →
`<models[].name>@<vendor-id>`，网关据此复用同一份 provider 配置并用 `model_overrides`
换掉 vendor 侧的模型名。厂商 API 不可用（没网/没 key/限流）时整体回退到配置目录，
输入框不会因此空掉。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from agentkit.config import AppConfig, ModelConfig
from agentkit.config.model_config import ReasoningEffortOption

logger = logging.getLogger(__name__)

#: 厂商和 config 名字之间的分隔符。@ 不在 models[].name 的字符集里，不会歧义。
VENDOR_SEPARATOR = "@"

#: 厂商 profile 只给 id 时**原样显示 id**（不改大小写）；只有 `none` 这一档在
#: 输入框里统一叫 `Off`。
EFFORT_LABELS = {
    "none": "Off",
}

#: 厂商名推导表：`langchain_deepseek` → `deepseek` → `DeepSeek`。
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "azure": "Azure OpenAI",
    "qwen": "通义千问",
    "dashscope": "通义千问",
    "google": "Google",
    "bedrock": "Amazon Bedrock",
}

CATALOG_CACHE_TTL_SECONDS = 300.0


@dataclass(slots=True)
class CatalogModel:
    """一条可选模型。name 是回传给网关的唯一标识，model 是厂商侧 id。"""

    name: str
    model: str
    provider: str
    display_name: str | None
    description: str | None
    context_window: int | None
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    reasoning_efforts: list[ReasoningEffortOption] = field(default_factory=list)
    default_reasoning_effort: str | None = None
    source: Literal["config", "vendor"] = "config"
    available: bool = True


@dataclass(slots=True)
class CatalogResult:
    models: list[CatalogModel]
    default_model: str | None
    default_effort: str | None = None
    vendor_errors: list[str] = field(default_factory=list)


_cache: dict[str, tuple[float, CatalogResult]] = {}


def provider_label(cfg: ModelConfig) -> str:
    """厂商展示名：配置优先，其次从 use 的模块名推导（langchain_deepseek → DeepSeek）。"""

    if cfg.provider and cfg.provider.strip():
        return cfg.provider.strip()
    # use 有两种写法：`langchain_deepseek:ChatDeepSeek`（模块:类）和
    # `langchain_openai.ChatOpenAI`（点号路径）。两种都要能推出 `deepseek` / `openai`。
    reference = cfg.use.split(":", 1)[0]
    module = reference.rsplit(".", 1)[0] if "." in reference else reference
    name = module.rsplit(".", 1)[-1]
    if name.startswith("langchain_"):
        name = name[len("langchain_") :]
    key = name.lower()
    return PROVIDER_LABELS.get(key, name.replace("_", " ").title() or "provider")


def split_model_name(model_name: str) -> tuple[str, str | None]:
    entry, _, vendor = model_name.partition(VENDOR_SEPARATOR)
    return entry, (vendor or None)


def vendor_model_name(entry: str, vendor_id: str) -> str:
    return f"{entry}{VENDOR_SEPARATOR}{vendor_id}"


def _auth_headers(cfg: ModelConfig) -> dict[str, str]:
    key = cfg.api_key or ""
    if not key or key.startswith("$"):
        return {}
    return {"Authorization": f"Bearer {key}"}


def _models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/models"):
        return base
    return base + "/models"


async def fetch_vendor_model_ids(
    cfg: ModelConfig, *, client: httpx.AsyncClient, timeout: float
) -> list[str]:
    """`GET {base_url}/models`：OpenAI 兼容的目录接口，DeepSeek 也支持。

    只取 id；别的字段（owner/created）不保证存在，也和我们无关。
    """

    response = await client.get(
        _models_url(cfg.base_url or ""), headers=_auth_headers(cfg), timeout=timeout
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload: Any = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("unexpected payload")
    ids: list[str] = []
    for row in rows:
        value = row.get("id") if isinstance(row, dict) else row
        if isinstance(value, str) and value.strip() and value not in ids:
            ids.append(value.strip())
    if not ids:
        raise RuntimeError("no models")
    return ids


def _profile_efforts(cfg: ModelConfig, app_config: AppConfig) -> list[ReasoningEffortOption]:
    """厂商集成自带的 model profile（openai 的 profile_augmentations 就在这里）。"""

    try:
        from agentkit.models.factory import create_chat_model

        model = create_chat_model(name=cfg.name, app_config=app_config, attach_tracing=False)
        profile = getattr(model, "profile", None)
    except Exception:  # provider 包没装/配置不全：没有候选就不显示这一行
        logger.debug("no model profile for %s", cfg.name, exc_info=True)
        return []
    levels = profile.get("reasoning_effort_levels") if isinstance(profile, dict) else None
    if not isinstance(levels, list):
        return []
    default = profile.get("reasoning_effort_default") if isinstance(profile, dict) else None
    options: list[ReasoningEffortOption] = []
    for level in levels:
        if not isinstance(level, str) or not level.strip():
            continue
        options.append(
            ReasoningEffortOption(
                id=level,
                label=EFFORT_LABELS.get(level.lower()),
                default=bool(default) and level == default,
            )
        )
    return options


def reasoning_efforts_for(cfg: ModelConfig, app_config: AppConfig) -> list[ReasoningEffortOption]:
    """配置写了文案就用配置；没写就问厂商集成要 profile。都没有 → 前端不显示这一段。"""

    if cfg.reasoning_efforts:
        return list(cfg.reasoning_efforts)
    return _profile_efforts(cfg, app_config)


def _default_effort(options: list[ReasoningEffortOption]) -> str | None:
    for option in options:
        if option.default:
            return option.id
    return None


async def build_model_catalog(
    app_config: AppConfig,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 3.0,
    use_cache: bool = True,
) -> CatalogResult:
    """厂商 API 为准，config 兜底。永远返回可用结果，不把异常抛给路由。"""

    cache_key = str(id(app_config))
    if use_cache:
        cached = _cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < CATALOG_CACHE_TTL_SECONDS:
            return cached[1]

    owns_client = client is None
    http = client or make_http_client(timeout)
    models: list[CatalogModel] = []
    errors: list[str] = []
    seen: set[str] = set()
    try:
        # 各 provider 并发问一次：串行会让"厂商很慢"直接变成"输入框很慢"
        vendor_lists = await asyncio.gather(
            *[
                fetch_vendor_model_ids(cfg, client=http, timeout=timeout) if cfg.base_url else _none()
                for cfg in app_config.models
            ],
            return_exceptions=True,
        )
        for cfg, vendor_result in zip(app_config.models, vendor_lists, strict=True):
            efforts = reasoning_efforts_for(cfg, app_config)
            provider = provider_label(cfg)
            vendor_ids: list[str] = []
            if isinstance(vendor_result, BaseException):
                errors.append(f"{provider}: {type(vendor_result).__name__}")
                logger.info(
                    "vendor model list unavailable for %s: %s", cfg.name, vendor_result
                )
            elif vendor_result:
                vendor_ids = list(vendor_result)
            if not vendor_ids:
                if cfg.name not in seen:
                    seen.add(cfg.name)
                    models.append(_entry_model(cfg, provider, efforts, source="config"))
                continue
            for vendor_id in vendor_ids:
                if vendor_id == cfg.model:
                    if cfg.name not in seen:
                        seen.add(cfg.name)
                        models.append(_entry_model(cfg, provider, efforts, source="vendor"))
                    continue
                name = vendor_model_name(cfg.name, vendor_id)
                # 名字唯一：同一个 name 在 config 里出现两次时只保留第一条（和以前一致）
                if name in seen:
                    continue
                seen.add(name)
                models.append(
                    CatalogModel(
                        name=name,
                        model=vendor_id,
                        provider=provider,
                        display_name=vendor_id,
                        description=None,
                        context_window=None,
                        supports_thinking=cfg.supports_thinking,
                        supports_reasoning_effort=bool(efforts) or cfg.supports_reasoning_effort,
                        supports_vision=False,
                        reasoning_efforts=list(efforts),
                        default_reasoning_effort=_default_effort(efforts),
                        source="vendor",
                    )
                )
    finally:
        if owns_client:
            await http.aclose()

    default_model = app_config.agents.default_model or (
        app_config.models[0].name if app_config.models else None
    )
    default_cfg = app_config.get_model_config(default_model) if default_model else None
    result = CatalogResult(
        models=models,
        default_model=default_model,
        default_effort=(
            _default_effort(reasoning_efforts_for(default_cfg, app_config)) if default_cfg else None
        ),
        vendor_errors=errors,
    )
    if use_cache:
        _cache[cache_key] = (time.monotonic(), result)
    return result


def make_http_client(timeout: float) -> httpx.AsyncClient:
    """厂商目录用的 HTTP 客户端。测试用 mock transport 替换这一个函数即可。"""

    return httpx.AsyncClient(timeout=timeout)


async def _none() -> list[str]:
    return []


def _entry_model(
    cfg: ModelConfig, provider: str, efforts: list[ReasoningEffortOption], *, source: str
) -> CatalogModel:
    """config 条目本身那一行（厂商也报了同一个 id 时标成 vendor 来源）。"""

    return CatalogModel(
        name=cfg.name,
        model=cfg.model,
        provider=provider,
        display_name=cfg.display_name,
        description=cfg.description,
        context_window=cfg.context_window,
        supports_thinking=cfg.supports_thinking,
        supports_reasoning_effort=bool(efforts) or cfg.supports_reasoning_effort,
        supports_vision=cfg.supports_vision,
        reasoning_efforts=efforts,
        default_reasoning_effort=_default_effort(efforts),
        source="vendor" if source == "vendor" else "config",
    )


def _as_dict(model: CatalogModel) -> dict[str, Any]:
    return {
        field: getattr(model, field)
        for field in (
            "name", "model", "provider", "display_name", "description", "context_window",
            "supports_thinking", "supports_reasoning_effort", "supports_vision",
            "reasoning_efforts", "default_reasoning_effort", "source", "available",
        )
    }


def clear_catalog_cache() -> None:
    _cache.clear()
