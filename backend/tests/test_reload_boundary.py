from typing import Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from agentkit.config.app_config import AppConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX


def _nested_model(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) is Union:
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _nested_model(arg)
            if found is not None:
                return found
    return None


def _iter_schema_fields(model: type[BaseModel], prefix: str = ""):
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        yield path, field
        nested = _nested_model(field.annotation)
        if nested is not None and nested is not model:
            yield from _iter_schema_fields(nested, path)


def test_startup_only_registry_matches_schema():
    fields: dict[str, FieldInfo] = dict(_iter_schema_fields(AppConfig))

    for path in STARTUP_ONLY_FIELDS:
        field = fields.get(path)
        if field is None:
            continue
        assert (field.description or "").startswith(STARTUP_ONLY_PREFIX), path

    for path, field in fields.items():
        if (field.description or "").startswith(STARTUP_ONLY_PREFIX):
            assert path in STARTUP_ONLY_FIELDS, (
                f"Field {path} is marked as startup-only but is not in STARTUP_ONLY_FIELDS"
            )