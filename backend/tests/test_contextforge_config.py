import pytest
from pydantic import ValidationError

from agentkit.config.app_config import AppConfig


def _base() -> dict:
    return {"models": [{"name": "m", "use": "x:Y", "model": "m"}]}


def test_contextforge_disabled_by_default() -> None:
    cfg = AppConfig.model_validate(_base())

    assert cfg.contextforge.enabled is False
    assert cfg.contextforge.tool_loading == "auto"
    assert cfg.contextforge.search.vector_store.collection == "myflow_tools_catalog"
    assert cfg.contextforge.admin_events.enabled is True


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


def test_non_eager_loading_requires_search() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                **_base(),
                "embedding": {"base_url": "http://embed", "dimensions": 8},
                "vector_store": {"uri": "http://milvus:19530"},
                "contextforge": {
                    "enabled": True,
                    "base_url": "http://cf",
                    "tool_loading": "deferred",
                    "search": {"enabled": False},
                },
            }
        )


def test_tool_collection_is_pinned() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                **_base(),
                "embedding": {"base_url": "http://embed", "dimensions": 8},
                "vector_store": {"uri": "http://milvus:19530"},
                "contextforge": {
                    "enabled": True,
                    "base_url": "http://cf",
                    "search": {
                        "enabled": True,
                        "vector_store": {"collection": "myflow_knowledge"},
                    },
                },
            }
        )
