"""Model catalog. Reads AppConfig; does not start runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from agentkit.config import get_app_config
from backend.app.gateway.deps import require_scope
from backend.app.gateway.errors import ModelNotFound
from backend.app.gateway.models.request import ModelName
from backend.app.gateway.models.response import ModelResponse, ModelsListResponse, model_to_wire
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelsListResponse)
async def list_models(
    caller: Annotated[RunCaller, Depends(require_scope("models:read"))],
) -> ModelsListResponse:
    del caller
    config = get_app_config()
    seen: set[str] = set()
    models: list[ModelResponse] = []
    for cfg in config.models:
        if cfg.name in seen:
            continue
        seen.add(cfg.name)
        models.append(model_to_wire(cfg))
    return ModelsListResponse(models=models)


@router.get("/{model_name}", response_model=ModelResponse)
async def get_model(
    model_name: ModelName,
    caller: Annotated[RunCaller, Depends(require_scope("models:read"))],
) -> ModelResponse:
    del caller
    cfg = get_app_config().get_model_config(model_name)
    if cfg is None:
        raise ModelNotFound(f"Model {model_name} not found")
    return model_to_wire(cfg)
