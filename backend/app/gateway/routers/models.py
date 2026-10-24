"""Model catalog. Reads AppConfig + the vendor API; never starts runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from agentkit.config import get_app_config
from agentkit.models.catalog import build_model_catalog
from backend.app.gateway.deps import require_scope
from backend.app.gateway.errors import ModelNotFound
from backend.app.gateway.models.request import ModelName
from backend.app.gateway.models.response import (
    ModelResponse,
    ModelsListResponse,
    catalog_model_to_wire,
    model_to_wire,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelsListResponse)
async def list_models(
    caller: Annotated[RunCaller, Depends(require_scope("models:read"))],
) -> ModelsListResponse:
    """模型列表与推理等级：**以厂商 API 为准**，config.yaml 提供建模型的配置与兜底。

    `GET {base_url}/models` 报出来的 id 全部可选：和 config 的 `models[].model` 相同的
    沿用该条目的 name，其余用 `<name>@<vendor-id>`（网关按同一份 provider 配置建模型）。
    厂商不可达时整体回退到配置目录，并在 `vendor_errors` 里说明原因。
    """

    del caller
    catalog = await build_model_catalog(get_app_config())
    return ModelsListResponse(
        models=[catalog_model_to_wire(model) for model in catalog.models],
        default_model=catalog.default_model,
        default_reasoning_effort=catalog.default_effort,
        vendor_errors=catalog.vendor_errors,
    )


@router.get("/{model_name}", response_model=ModelResponse)
async def get_model(
    model_name: ModelName,
    caller: Annotated[RunCaller, Depends(require_scope("models:read"))],
) -> ModelResponse:
    """单个配置模型。厂商新报出来的 `<name>@<vendor-id>` 用列表接口查（路径不允许 @）。"""

    del caller
    cfg = get_app_config().get_model_config(model_name)
    if cfg is None:
        raise ModelNotFound(f"Model {model_name} not found")
    return model_to_wire(cfg)
