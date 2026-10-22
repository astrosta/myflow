"""Rerank client: protocol shapes, retries, degradation signals, and config gating."""

from __future__ import annotations

import httpx
import pytest
from agentkit.knowledge.config import RerankConfig
from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.rerank import OpenAICompatibleReranker, build_reranker


def _config(**overrides) -> RerankConfig:
    base = {
        "enabled": True,
        "base_url": "http://rerank.test/",
        "model": "Qwen/Qwen3-Reranker-8B",
    }
    base.update(overrides)
    return RerankConfig.model_validate(base)


def _client(handler, requests: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def test_build_reranker_is_none_when_disabled() -> None:
    assert build_reranker(RerankConfig()) is None
    assert build_reranker(_config()) is not None


@pytest.mark.asyncio
async def test_cohere_protocol_sends_documents_and_sorts_desc() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    reranker = OpenAICompatibleReranker(_config(), client=_client(handler, requests))
    scores = await reranker.rerank(query="升级步骤", documents=["a", "b"])
    assert [item.index for item in scores] == [1, 0]
    assert scores[0].score == 0.9
    body = requests[0].read().decode()
    assert '"model":"Qwen/Qwen3-Reranker-8B"' in body
    assert '"documents":["a","b"]' in body
    assert str(requests[0].url) == "http://rerank.test/rerank"


@pytest.mark.asyncio
async def test_tei_protocol_uses_texts_and_plain_list_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 1, "score": 0.8}, {"index": 0, "score": 0.1}])

    reranker = OpenAICompatibleReranker(
        _config(protocol="tei"), client=_client(handler, requests)
    )
    scores = await reranker.rerank(query="q", documents=["a", "b"])
    assert [item.index for item in scores] == [1, 0]
    body = requests[0].read().decode()
    assert '"texts":["a","b"]' in body
    assert '"model"' not in body


@pytest.mark.asyncio
async def test_documents_are_clipped_before_the_request() -> None:
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]})

    reranker = OpenAICompatibleReranker(
        _config(max_document_chars=200), client=_client(handler)
    )
    await reranker.rerank(query="q", documents=["x" * 500])
    assert "x" * 500 not in bodies[0]
    assert "x" * 200 in bodies[0]


@pytest.mark.asyncio
async def test_http_error_and_bad_payload_raise_unavailable() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    reranker = OpenAICompatibleReranker(_config(max_retries=0), client=_client(failing))
    with pytest.raises(KnowledgeUnavailable, match="HTTP 500"):
        await reranker.rerank(query="q", documents=["a"])

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    reranker2 = OpenAICompatibleReranker(_config(), client=_client(garbage))
    with pytest.raises(KnowledgeUnavailable):
        await reranker2.rerank(query="q", documents=["a"])


@pytest.mark.asyncio
async def test_out_of_range_and_duplicate_indices_are_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 9, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
        )

    reranker = OpenAICompatibleReranker(_config(), client=_client(handler))
    scores = await reranker.rerank(query="q", documents=["a"])
    assert [(item.index, item.score) for item in scores] == [(0, 0.5)]


@pytest.mark.asyncio
async def test_empty_input_never_calls_the_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    reranker = OpenAICompatibleReranker(_config(), client=_client(handler))
    assert await reranker.rerank(query="q", documents=[]) == []
    assert await reranker.rerank(query="   ", documents=["a"]) == []
