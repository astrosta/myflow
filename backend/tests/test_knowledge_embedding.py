"""Qwen embedding client: batching, retry, protocol and dimension checks."""

from __future__ import annotations

import json

import httpx
import pytest
from agentkit.knowledge.config import EmbeddingConfig
from agentkit.knowledge.embedding import OpenAICompatibleEmbedder
from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.tokenizer import ApproximateTokenCounter


def _config(**overrides: object) -> EmbeddingConfig:
    base = {
        "base_url": "http://embedding.test/v1",
        "model": "Qwen/Qwen3-Embedding-8B",
        "dimensions": 4,
        "batch_size": 2,
        "max_input_tokens": 100,
        "query_instruction": "Retrieve passages",
    }
    base.update(overrides)
    return EmbeddingConfig.model_validate(base)


@pytest.mark.asyncio
async def test_openai_embeddings_are_batched_and_query_instruction_is_prefixed() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        vectors = [
            [float(index + 1), 1.0, 0.0, 0.0]
            for index, _ in enumerate(payload["input"])
        ]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(_config(), client=client, token_counter=ApproximateTokenCounter())
    vectors = await embedder.embed_documents(["doc one", "doc two", "doc three"])
    query = await embedder.embed_query("question")
    await embedder.aclose()
    await client.aclose()

    assert len(vectors) == 3
    assert [len(payload["input"]) for payload in payloads[:2]] == [2, 1]
    assert payloads[-1]["input"][0].startswith("Instruct: Retrieve passages\nQuery:")
    assert query and len(query) == 4


@pytest.mark.asyncio
async def test_retry_then_success_and_dimension_failure() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(_config(max_retries=1), client=client, token_counter=ApproximateTokenCounter())
    with pytest.raises(KnowledgeUnavailable, match="dimension"):
        await embedder.embed_documents(["doc"])
    assert attempts == 2
    await embedder.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_tei_protocol_uses_embed_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0, 0.0]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(
        _config(
            protocol="tei",
            base_url="http://embedding.test",
            path="/embed",
            query_instruction="",
        ),
        client=client,
        token_counter=ApproximateTokenCounter(),
    )
    vector = await embedder.embed_query("question")
    await embedder.aclose()
    await client.aclose()
    assert seen["path"] == "/embed"
    assert seen["body"]["inputs"] == ["question"]
    assert vector == [1.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_input_too_long_is_rejected_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(
        _config(max_input_tokens=1), client=client, token_counter=ApproximateTokenCounter()
    )
    with pytest.raises(KnowledgeUnavailable, match="max_input_tokens"):
        await embedder.embed_documents(["this is too long"])
    assert calls == 0
    await embedder.aclose()
    await client.aclose()
