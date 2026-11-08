"""Shared embedding/rerank/Milvus defaults for knowledge and skill search."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from agentkit.config import AppConfig
from pydantic import ValidationError


def _models() -> list[dict]:
    return [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}]


def _config(**sections: object) -> AppConfig:
    return AppConfig.model_validate({"models": _models(), **sections})


def test_shared_services_fill_both_consumers_without_overwriting_policies() -> None:
    cfg = _config(
        embedding={
            "base_url": "http://embedding:8000/v1",
            "model": "shared-embedding",
            "dimensions": 1024,
            "max_input_tokens": 2048,
        },
        rerank={
            "base_url": "http://rerank:8000",
            "model": "shared-reranker",
        },
        vector_store={
            "uri": "http://milvus:19530",
            "token": "secret",
            "db_name": "shared",
        },
        knowledge_base={
            "embedding": {"timeout_seconds": 11},
            "retrieval": {"rerank": {"timeout_seconds": 12}},
        },
        skills={
            "search": {
                "embedding": {"timeout_seconds": 7},
                "rerank": {"timeout_seconds": 9},
            }
        },
    )

    for embedding in (cfg.knowledge_base.embedding, cfg.skills.search.embedding):
        assert embedding.base_url == "http://embedding:8000/v1"
        assert embedding.model == "shared-embedding"
        assert embedding.dimensions == 1024
        assert embedding.max_input_tokens == 2048

    # Instructions and runtime policies stay consumer-specific.
    assert cfg.knowledge_base.embedding.query_instruction.startswith("Given a question")
    assert cfg.skills.search.embedding.query_instruction.startswith("Given a task")
    assert cfg.knowledge_base.embedding.timeout_seconds == 11
    assert cfg.skills.search.embedding.timeout_seconds == 7

    assert cfg.knowledge_base.retrieval.rerank.base_url == "http://rerank:8000"
    assert cfg.skills.search.rerank.model == "shared-reranker"
    assert cfg.knowledge_base.retrieval.rerank.timeout_seconds == 12
    assert cfg.skills.search.rerank.timeout_seconds == 9

    assert cfg.knowledge_base.vector_store.uri == "http://milvus:19530"
    assert cfg.skills.search.vector_store.uri == "http://milvus:19530"
    assert cfg.knowledge_base.vector_store.token == "secret"
    assert cfg.skills.search.vector_store.db_name == "shared"
    assert cfg.knowledge_base.vector_store.collection == "myflow_knowledge"
    assert cfg.skills.search.vector_store.collection == "myflow_skills_active"
    assert cfg.knowledge_base.vector_store.consistency_level == "Bounded"
    assert cfg.skills.search.vector_store.consistency_level == "Bounded"


def test_blank_vector_store_credentials_match_config_yaml() -> None:
    """config.yaml 把 token / db_name 留空，YAML 读成 null，表示不鉴权、用默认库。"""
    cfg = _config(
        vector_store={"uri": "http://milvus:19530", "token": None, "db_name": None},
        knowledge_base={"vector_store": {"collection": "myflow_knowledge"}},
        skills={"search": {"vector_store": {"collection": "myflow_skills_active"}}},
    )

    for store in (
        cfg.vector_store,
        cfg.knowledge_base.vector_store,
        cfg.skills.search.vector_store,
    ):
        assert store.token == ""
        assert store.db_name == ""


def test_consumer_sections_override_shared_values() -> None:
    cfg = _config(
        embedding={"base_url": "http://common", "model": "common-embedding"},
        rerank={"base_url": "http://common-rerank", "model": "common-reranker"},
        vector_store={"uri": "http://common-milvus", "token": "common"},
        knowledge_base={
            "embedding": {"base_url": "http://knowledge-embedding"},
            "vector_store": {"uri": "http://knowledge-milvus", "collection": "kb_collection"},
            "retrieval": {"rerank": {"base_url": "http://knowledge-rerank"}},
        },
        skills={
            "search": {
                "embedding": {"model": "skills-embedding"},
                "vector_store": {"token": "skills-token"},
                "rerank": {"model": "skills-reranker"},
            }
        },
    )

    assert cfg.knowledge_base.embedding.base_url == "http://knowledge-embedding"
    assert cfg.knowledge_base.embedding.model == "common-embedding"
    assert cfg.skills.search.embedding.base_url == "http://common"
    assert cfg.skills.search.embedding.model == "skills-embedding"

    assert cfg.knowledge_base.retrieval.rerank.base_url == "http://knowledge-rerank"
    assert cfg.knowledge_base.retrieval.rerank.model == "common-reranker"
    assert cfg.skills.search.rerank.base_url == "http://common-rerank"
    assert cfg.skills.search.rerank.model == "skills-reranker"

    assert cfg.knowledge_base.vector_store.uri == "http://knowledge-milvus"
    assert cfg.knowledge_base.vector_store.token == "common"
    assert cfg.knowledge_base.vector_store.collection == "kb_collection"
    assert cfg.skills.search.vector_store.uri == "http://common-milvus"
    assert cfg.skills.search.vector_store.token == "skills-token"


def test_shared_addresses_satisfy_enabled_preflights() -> None:
    cfg = _config(
        embedding={"base_url": "http://embedding:8000/v1"},
        vector_store={"uri": "http://milvus:19530"},
        knowledge_base={
            "enabled": True,
            "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
        },
        skills={"search": {"embedding": {"enabled": True}}},
    )

    assert cfg.knowledge_base.enabled is True
    assert cfg.knowledge_base.embedding.base_url == "http://embedding:8000/v1"
    assert cfg.knowledge_base.vector_store.uri == "http://milvus:19530"
    assert cfg.skills.search.embedding.enabled is True


def test_shared_sections_reject_consumer_only_fields() -> None:
    with pytest.raises(ValidationError, match="query_instruction"):
        _config(embedding={"base_url": "http://embedding:8000", "query_instruction": "x"})


def test_example_config_keeps_common_values_at_the_top_level() -> None:
    path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["embedding"]["model"] == "Qwen/Qwen3-Embedding-8B"
    assert data["rerank"]["model"] == "Qwen/Qwen3-Reranker-8B"
    assert data["vector_store"]["provider"] == "milvus"
    assert "base_url" not in data["skills"]["search"]["embedding"]
    assert "base_url" not in data["knowledge_base"]["embedding"]
    assert "uri" not in data["skills"]["search"]["vector_store"]
    assert "uri" not in data["knowledge_base"]["vector_store"]
    assert "model" not in data["knowledge_base"]["retrieval"]["rerank"]
