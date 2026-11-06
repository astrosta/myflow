"""Knowledge-base configuration: enabled preflight, type chunking, token budgets."""

from __future__ import annotations

import pytest
from agentkit.config import AppConfig
from agentkit.knowledge.config import KnowledgeBaseConfig
from agentkit.knowledge.types import KNOWLEDGE_BASE_TYPES
from pydantic import ValidationError


def _models() -> list[dict]:
    return [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


def test_disabled_by_default_and_appconfig_accepts_section() -> None:
    cfg = AppConfig.model_validate({"models": _models()})
    assert cfg.knowledge_base.enabled is False

    configured = AppConfig.model_validate(
        {
            "models": _models(),
            "knowledge_base": {
                "enabled": False,
                "storage_dir": ".myflow/knowledge-test",
            },
        }
    )
    assert configured.knowledge_base.storage_dir == ".myflow/knowledge-test"


def test_enabled_requires_every_service_address() -> None:
    with pytest.raises(ValidationError) as excinfo:
        KnowledgeBaseConfig.model_validate({"enabled": True, "parse": {"mode": "local"}})
    message = str(excinfo.value)
    assert "parse.local.endpoint" in message
    assert "embedding.base_url" in message
    assert "vector_store.uri" in message


def test_enabled_cloud_requires_cloud_base_url_and_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        KnowledgeBaseConfig.model_validate(
            {
                "enabled": True,
                "parse": {"mode": "cloud"},
                "embedding": {"base_url": "http://embedding:8000/v1"},
                "vector_store": {"uri": "http://milvus:19530"},
            }
        )
    message = str(excinfo.value)
    assert "parse.cloud.base_url" in message
    assert "parse.cloud.api_key" in message


def test_retrieval_defaults_enable_keyword_and_disable_rerank() -> None:
    retrieval = KnowledgeBaseConfig().retrieval
    assert retrieval.keyword.enabled is True
    assert retrieval.keyword.analyzer == "chinese"
    assert retrieval.rrf_k == 60
    assert retrieval.rerank.enabled is False
    assert retrieval.rerank.protocol == "cohere"
    assert retrieval.rerank.model == "Qwen/Qwen3-Reranker-8B"


def test_enabling_rerank_requires_an_address_and_a_model() -> None:
    base = {
        "enabled": True,
        "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
        "embedding": {"base_url": "http://embedding:8000/v1"},
        "vector_store": {"uri": "http://milvus:19530"},
    }
    with pytest.raises(ValidationError) as excinfo:
        KnowledgeBaseConfig.model_validate(
            {**base, "retrieval": {"rerank": {"enabled": True}}}
        )
    message = str(excinfo.value)
    assert "retrieval.rerank.base_url" in message

    with pytest.raises(ValidationError) as excinfo:
        KnowledgeBaseConfig.model_validate(
            {
                **base,
                "retrieval": {"rerank": {"enabled": True, "base_url": "http://r:8000", "model": " "}},
            }
        )
    assert "retrieval.rerank.model" in str(excinfo.value)

    ok = KnowledgeBaseConfig.model_validate(
        {
            **base,
            "retrieval": {
                "keyword": {"enabled": False},
                "rerank": {"enabled": True, "base_url": "http://r:8000", "model": "m"},
            },
        }
    )
    assert ok.retrieval.rerank.enabled is True
    assert ok.retrieval.keyword.enabled is False


def test_mineru_endpoint_paths_are_configurable_and_relative() -> None:
    from agentkit.knowledge.config import CloudMinerUConfig, LocalMinerUConfig

    cloud = CloudMinerUConfig(
        base_url="http://cloud.test",
        file_urls_path="proxy/v4/file-urls",
        extract_results_path="/proxy/v4/batch/{batch_id}",
    )
    assert cloud.file_urls_path == "/proxy/v4/file-urls"
    assert cloud.extract_results_path == "/proxy/v4/batch/{batch_id}"

    local = LocalMinerUConfig(endpoint="http://mineru:8000", file_parse_path="v1/parse")
    assert local.file_parse_path == "/v1/parse"

    with pytest.raises(ValidationError) as excinfo:
        CloudMinerUConfig(extract_results_path="/api/v4/extract-results/batch")
    assert "batch_id" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        CloudMinerUConfig(file_urls_path="https://other.host/api/v4/file-urls/batch")
    assert "path" in str(excinfo.value)


def test_full_local_config_uses_token_defaults() -> None:
    cfg = KnowledgeBaseConfig.model_validate(
        {
            "enabled": True,
            "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
            "embedding": {"base_url": "http://embedding:8000/v1"},
            "vector_store": {"uri": "http://milvus:19530"},
        }
    )
    assert cfg.embedding.model == "Qwen/Qwen3-Embedding-8B"
    assert cfg.embedding.dimensions == 4096
    assert cfg.chunking.types["technical"].chunk_size_tokens == 512
    assert cfg.chunking.types["technical"].chunk_overlap_tokens == 64
    assert cfg.chunking.types["faq"].chunk_size_tokens == 384
    assert cfg.chunking.types["faq"].chunk_overlap_tokens == 0
    assert cfg.chunking.types["api"].chunk_size_tokens == 768
    assert set(cfg.chunking.types) == set(KNOWLEDGE_BASE_TYPES)


def test_chunking_has_no_tokenizer_config() -> None:
    """Token accounting is built in; the old tokenizer section must not return."""

    assert "tokenizer" not in KnowledgeBaseConfig().chunking.model_dump()
    with pytest.raises(ValidationError, match="tokenizer"):
        KnowledgeBaseConfig.model_validate(
            {"chunking": {"tokenizer": {"provider": "approximate"}}}
        )


def test_overlap_must_be_smaller_than_chunk_and_max_covers_all_types() -> None:
    with pytest.raises(ValidationError, match="chunk_overlap_tokens"):
        KnowledgeBaseConfig.model_validate(
            {
                "chunking": {
                    "types": {
                        "technical": {"chunk_size_tokens": 128, "chunk_overlap_tokens": 128}
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="max_chunk_tokens"):
        KnowledgeBaseConfig.model_validate(
            {
                "chunking": {
                    "max_chunk_tokens": 256,
                    "types": {
                        "api": {"chunk_size_tokens": 512, "chunk_overlap_tokens": 0}
                    },
                }
            }
        )


def test_text_extensions_must_be_allowed() -> None:
    with pytest.raises(ValidationError, match="text_extensions"):
        KnowledgeBaseConfig.model_validate(
            {
                "allowed_extensions": ["pdf"],
                "text_extensions": ["md"],
            }
        )
