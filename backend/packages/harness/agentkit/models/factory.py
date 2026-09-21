from __future__ import annotations
import logging
from agentkit.config.app_config import AppConfig, get_app_config
from agentkit.reflection.resolvers import resolve_class
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

_METADATA_FIELDS = frozenset({
    "use", "name", "display_name", "description",
    "supports_thinking", "supports_reasoning_effort",
    "when_thinking_enabled", "when_thinking_disabled", "thinking",
    "supports_vision", "context_window", "pricing",
})

_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS = 240.0

def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    merged = dict(base or {})

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged.get(key), value)
        else:
            merged[key] = value
    return merged

def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    disable: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable["enable_thinking"] = False
    return disable

def _is_openai_compatible(model_class: type) -> bool:
    try:
        from langchain_openai.chat_models.base import BaseChatOpenAI
    except ImportError:
        return False
    return issubclass(model_class, BaseChatOpenAI)

def _apply_stream_chunk_timeout_default(model_class: type, settings: dict) -> None:
    if not _is_openai_compatible(model_class):
        settings.pop("stream_chunk_timeout", None)
        return
    if "stream_chunk_timeout" in settings:
        return
    settings["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS


def _apply_stream_usage_default(model_class: type, settings: dict, kwargs: dict) -> None:
    if "stream_usage" in settings or "stream_usage" in kwargs:
        return
    if "stream_usage" in getattr(model_class, "model_fields", {}):
        settings["stream_usage"] = True


def _warn_unknow_model_settings(model_class: type, model_name: str, settings: dict) -> None:
    if not _is_openai_compatible(model_class):
        return
    known = getattr(model_class, "model_fields", None)
    if not known:
        return
    # 添加别名
    valid = set(known)
    for field in known.values():
        alias = getattr(field, "alias", None)
        if alias:
            valid.add(alias)

    valid |= {
        "model", "model_kwargs", "extra_body", "default_headers",
        "default_query", "stream_usage", "stream_chunk_timeout", "reasoning_effort",
    }

    unknown = sorted(k for k in settings if k not in valid)
    if unknown:
        logger.warning(
            "Model '%s' (%s): config key(s) %s are not recognized parameters "
            "of the model class and will be forwarded as-is; this may raise at "
            "request time. Check for typos (e.g. 'maxx_tokens' -> 'max_tokens').",
            model_name, getattr(model_class, "__name__", "?"), unknown,
        )

def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    attach_tracing: bool = True,
    model_overrides: dict | None = None,
    **kwargs,
) -> BaseChatModel:

    # get the model config
    config = app_config or get_app_config()

    # name解析，取ModelConfig
    if name is None:
        name = config.models[0].name

    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model config not found for name: {name}")
    
    # 获取模型类
    model_class = resolve_class(model_config.use, BaseChatModel)

    model_settings = model_config.model_dump(exclude_none=True, exclude=_METADATA_FIELDS)

    #调用方覆盖，暂时留空
    if model_overrides:
        model_settings.update({k:v for k, v in model_overrides.items() if v is not None})

    has_thinking_settings = (
        model_config.when_thinking_enabled is not None 
        or model_config.thinking is not None
    )

    # 设置thinking模式对应的配置
    effective_wte: dict = dict(model_config.when_thinking_enabled or {})
    if model_config.thinking is not None:
        effective_wte["thinking"] = {
            **(effective_wte.get("thinking", {})),
            **model_config.thinking,
        }
    
    if thinking_enabled:
        if has_thinking_settings and not model_config.supports_thinking:
            raise ValueError(
                f"Model {name} does not support thinking. "
                "Set `supports_thinking` to true in config.yaml."
            )
        if has_thinking_settings:
            model_settings = _deep_merge_dicts(model_settings, effective_wte)
    else:
        if model_config.when_thinking_disabled is not None:
            model_settings.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            model_settings["extra_body"] = _deep_merge_dicts(model_settings.get("extra_body"), {"thinking": {"type": "disabled"}})
            model_settings["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (
            disable := _vllm_disable_chat_template_kwargs(effective_wte.get("extra_body", {}).get("chat_template_kwargs", {}))):
            model_settings["extra_body"] = _deep_merge_dicts(model_settings.get("extra_body"), {"chat_template_kwargs": disable})
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            model_settings["thinking"] = {"type": "disabled"}

    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings.pop("reasoning_effort", None)

    # 若支持stream chunk超时，设置stream chunk默认超时
    _apply_stream_chunk_timeout_default(model_class, model_settings)
    _apply_stream_usage_default(model_class, model_settings, kwargs)
    _warn_unknow_model_settings(model_class, name, model_settings)

    translate_context_window = (
        bool(model_config.context_window)
        and "profile" not in kwargs
        and "profile" not in model_settings
    )
    model_instance = model_class(**kwargs, **model_settings)

    if translate_context_window:
        inferred = getattr(model_instance, "profile", None)
        model_instance.profile = {
            **(inferred or {}),
            "max_input_tokens": model_config.context_window,
        }

    return model_instance