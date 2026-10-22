"""Cross-encoder rerank stage (optional, enabled from config).

Rerank is a precision boost, never a correctness requirement: every failure raises
KnowledgeUnavailable so the caller can fall back to the RRF fusion order and say so in the
response. Addresses and secrets only ever come from the config file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from agentkit.knowledge.config import RerankConfig
from agentkit.knowledge.errors import KnowledgeUnavailable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RerankScore:
    """One reranked document: "index" points back into the request document list."""

    index: int
    score: float


class Reranker(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def rerank(self, *, query: str, documents: Sequence[str]) -> list[RerankScore]: ...

    async def aclose(self) -> None: ...


class OpenAICompatibleReranker:
    """Cohere-style (documents) and TEI-style (texts) rerank endpoints."""

    enabled = True

    def __init__(self, config: RerankConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    async def rerank(self, *, query: str, documents: Sequence[str]) -> list[RerankScore]:
        if not documents or not query.strip():
            return []
        texts = [self._clip(document) for document in documents]
        response = await self._post_with_retries(query, texts)
        return self._parse_scores(response, len(texts))

    def _clip(self, document: str) -> str:
        limit = self._config.max_document_chars
        return document if len(document) <= limit else document[:limit]

    async def _post_with_retries(self, query: str, documents: list[str]) -> httpx.Response:
        url = self._config.base_url.rstrip("/") + "/" + self._config.path.lstrip("/")
        headers = {"Authorization": f"Bearer {self._config.api_key}"} if self._config.api_key else {}
        if self._config.protocol == "tei":
            body: dict[str, Any] = {"query": query, "texts": documents}
        else:
            body = {
                "model": self._config.model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            }
        last_error = "unknown"
        for attempt in range(self._config.max_retries + 1):
            try:
                response = await self._http().post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            else:
                if response.status_code < 400:
                    return response
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in {408, 409, 429} and response.status_code < 500:
                    break
            if attempt < self._config.max_retries:
                await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
        raise KnowledgeUnavailable(f"rerank service request failed: {last_error}")

    def _parse_scores(self, response: httpx.Response, expected: int) -> list[RerankScore]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeUnavailable("rerank service returned invalid JSON") from exc
        raw: Any = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise KnowledgeUnavailable("rerank response has an unsupported shape")
        scores: list[RerankScore] = []
        seen: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise KnowledgeUnavailable("rerank response contains a non-object item")
            value = item.get("relevance_score", item.get("score"))
            try:
                position = int(item.get("index"))
                score = float(value)
            except (TypeError, ValueError) as exc:
                raise KnowledgeUnavailable("rerank response item has no index/score") from exc
            if position < 0 or position >= expected or position in seen:
                continue
            seen.add(position)
            scores.append(RerankScore(index=position, score=score))
        if not scores:
            raise KnowledgeUnavailable("rerank response has no usable scores")
        scores.sort(key=lambda item: item.score, reverse=True)
        return scores

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def build_reranker(
    config: RerankConfig, *, client: httpx.AsyncClient | None = None
) -> Reranker | None:
    """None means the stage is switched off in config; callers keep the fusion order."""

    if not config.enabled:
        return None
    return OpenAICompatibleReranker(config, client=client)
