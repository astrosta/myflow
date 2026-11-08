"""The final contextforge config shape (rev.9 12) and its load-time guards.

The retired rev.8 knobs (`tool_loading`, `deferred_threshold`) are gone: every
ContextForge tool is reached through tool_search + tool_execute, so there is
nothing left for a deferred/eager switch to decide.  What remains is the sync
schedule, the /admin/events subscription, the search budgets, and the fixed
collection - and each of those fails at load time rather than at 23:59 or on
the first search.
"""

import pytest
from pydantic import ValidationError

from agentkit.config.app_config import AppConfig
from agentkit.config.contextforge_config import TOOL_COLLECTION
from backend.app.gateway.app import preflight_contextforge


def _base() -> dict:
    return {"models": [{"name": "m", "use": "x:Y", "model": "m"}]}


def _enabled(search: dict | None = None) -> dict:
    return {
        **_base(),
        "embedding": {"base_url": "http://embed", "dimensions": 8},
        "vector_store": {"uri": "http://milvus:19530"},
        "contextforge": {
            "enabled": True,
            "base_url": "http://cf",
            "search": search if search is not None else {"enabled": True},
        },
    }


def test_contextforge_disabled_by_default() -> None:
    cfg = AppConfig.model_validate(_base())

    section = cfg.contextforge
    assert section.enabled is False
    assert section.admin_events.enabled is True
    assert section.sync.daily_at == "00:00"
    assert section.sync.timezone == "local"
    assert section.sync.daily_jitter_seconds == 60
    assert section.search.enabled is True
    assert section.search.vector_store.collection == TOOL_COLLECTION
    assert TOOL_COLLECTION == "myflow_contextforge_tools"
    assert section.search.max_schema_chars == 3000
    assert section.search.max_result_chars == 16000
    assert section.search.require_retrieved is True
    assert section.search.max_retrieved_tools == 5


def test_the_retired_tool_loading_switches_are_rejected() -> None:
    for retired in ({"tool_loading": "auto"}, {"deferred_threshold": 24}):
        with pytest.raises(ValidationError):
            AppConfig.model_validate(
                {**_base(), "contextforge": {"enabled": False, **retired}}
            )


def test_shared_embedding_rerank_and_vector_store_reach_contextforge() -> None:
    cfg = AppConfig.model_validate(
        {
            **_base(),
            "embedding": {"base_url": "http://embed", "dimensions": 8},
            "rerank": {"base_url": "http://rerank", "model": "r"},
            "vector_store": {"uri": "http://milvus:19530"},
            "contextforge": {
                "enabled": True,
                "base_url": "http://cf",
                "search": {"enabled": True},
            },
        }
    )

    assert cfg.contextforge.search.embedding.base_url == "http://embed"
    assert cfg.contextforge.search.rerank.base_url == "http://rerank"
    assert cfg.contextforge.search.vector_store.uri == "http://milvus:19530"


def test_search_requires_an_embedding_service() -> None:
    payload = _enabled()
    del payload["embedding"]

    with pytest.raises(ValidationError, match="embedding.base_url"):
        AppConfig.model_validate(payload)


def test_search_requires_the_milvus_instance() -> None:
    payload = _enabled()
    del payload["vector_store"]

    with pytest.raises(ValidationError, match="vector_store.uri"):
        AppConfig.model_validate(payload)


def test_keyword_only_search_does_not_require_an_embedding_service() -> None:
    cfg = AppConfig.model_validate(
        {
            **_base(),
            "vector_store": {"uri": "http://milvus:19530"},
            "contextforge": {
                "enabled": True,
                "base_url": "http://cf",
                "search": {
                    "enabled": True,
                    "keyword": {"enabled": True},
                    "embedding": {"enabled": False},
                },
            },
        }
    )

    assert cfg.contextforge.search.keyword.enabled is True
    assert cfg.contextforge.search.embedding.enabled is False
    assert cfg.contextforge.search.embedding.base_url == ""


def test_search_requires_at_least_one_recall_channel() -> None:
    with pytest.raises(ValidationError, match="recall channel"):
        AppConfig.model_validate(
            _enabled({"enabled": True, "keyword": {"enabled": False}, "embedding": {"enabled": False}})
        )


def test_the_tool_collection_is_pinned() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            _enabled({"enabled": True, "vector_store": {"collection": "myflow_knowledge"}})
        )


def test_daily_schedule_and_timezone_are_validated_at_load_time() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**_base(), "contextforge": {"sync": {"daily_at": "25:00"}}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {**_base(), "contextforge": {"sync": {"timezone": "Mars/Olympus"}}}
        )

    cfg = AppConfig.model_validate(
        {**_base(), "contextforge": {"sync": {"daily_at": "03:30", "timezone": "Asia/Shanghai"}}}
    )
    assert cfg.contextforge.sync.daily_at == "03:30"
    assert cfg.contextforge.sync.timezone == "Asia/Shanghai"


def test_max_retrieved_tools_never_exceeds_the_five_card_cap() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_enabled({"enabled": True, "max_retrieved_tools": 6}))
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_enabled({"enabled": True, "max_retrieved_tools": 0}))

    # A smaller top_k simply returns fewer cards; the run-scoped cap stays at five.
    cfg = AppConfig.model_validate(
        _enabled({"enabled": True, "top_k": 3, "max_retrieved_tools": 5})
    )
    assert cfg.contextforge.search.max_retrieved_tools == 5


def test_the_retired_loader_and_snapshot_cache_cannot_be_imported() -> None:
    import importlib

    for module in (
        "agentkit.contextforge.cache",
        "agentkit.contextforge.loader",
        "agentkit.tools.builtin.search_tools",
        "agentkit.tools.contextforge_metadata",
        "agentkit.tools.mcp_metadata",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


def test_enabled_contextforge_refuses_a_multi_worker_deployment(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")
    cfg = AppConfig.model_validate(_enabled())

    with pytest.raises(RuntimeError, match="GATEWAY_WORKERS"):
        preflight_contextforge(cfg)

    monkeypatch.setenv("GATEWAY_WORKERS", "1")
    preflight_contextforge(cfg)
