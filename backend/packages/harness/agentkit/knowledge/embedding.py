"""Qwen3-Embedding-8B transport over OpenAI-compatible or TEI endpoints."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Sequence
from typing import Any

import httpx

from agentkit.knowledge.config import EmbeddingConfig
from agentkit.knowledge.errors import KnowledgeUnavailable
from agentkit.knowledge.tokenizer import ApproximateTokenCounter, TokenCounter

logger = logging.getLogger(__name__)


class OpenAICompatibleEmbedder:
    name = "openai_compatible"

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        client: httpx.AsyncClient | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._counter = token_counter or ApproximateTokenCounter()
        # 检索会在子代理的隔离循环上执行，gateway 主循环也在用同一个 embedder：
        # httpx 客户端绑定创建它的 loop，所以按 running loop 各缓存一份。
        self._loop_clients: weakref.WeakKeyDictionary[Any, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary()
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        loop = asyncio.get_running_loop()
        client = self._loop_clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
            self._loop_clients[loop] = client
        return client

    @property
    def dimensions(self) -> int:
        return self._config.dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [self._document_text(text) for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(prepared), self._config.batch_size):
            vectors.extend(await self._embed_batch(prepared[start : start + self._config.batch_size]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        prepared = [self._query_text(text)]
        return (await self._embed_batch(prepared))[0]

    def _document_text(self, text: str) -> str:
        instruction = self._config.document_instruction.strip()
        return f"Instruct: {instruction}\nDocument: {text}" if instruction else text

    def _query_text(self, text: str) -> str:
        instruction = self._config.query_instruction.strip()
        return f"Instruct: {instruction}\nQuery: {text}" if instruction else text

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        for text in texts:
            if self._counter.count(text) > self._config.max_input_tokens:
                raise KnowledgeUnavailable(
                    f"embedding input exceeds max_input_tokens ({self._config.max_input_tokens})"
                )
        response = await self._post_with_retries(texts)
        vectors = self._parse_vectors(response)
        if len(vectors) != len(texts):
            raise KnowledgeUnavailable("embedding response count does not match input count")
        for vector in vectors:
            if len(vector) != self._config.dimensions:
                raise KnowledgeUnavailable(
                    f"embedding dimension mismatch: expected {self._config.dimensions}, got {len(vector)}"
                )
        return vectors

    async def _post_with_retries(self, texts: list[str]) -> httpx.Response:
        path = self._config.path or ("/embed" if self._config.protocol == "tei" else "/embeddings")
        url = self._config.base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {"Authorization": f"Bearer {self._config.api_key}"} if self._config.api_key else {}
        if self._config.protocol == "tei":
            body: dict[str, Any] = {"inputs": texts}
        else:
            body = {"model": self._config.model, "input": texts}
            if self._config.send_dimensions:
                body["dimensions"] = self._config.dimensions
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
        raise KnowledgeUnavailable(f"embedding service request failed: {last_error}")

    def _parse_vectors(self, response: httpx.Response) -> list[list[float]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeUnavailable("embedding service returned invalid JSON") from exc
        raw: Any
        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                ordered = sorted(payload["data"], key=lambda item: int(item.get("index", 0)))
                raw = [item.get("embedding") for item in ordered]
            elif isinstance(payload.get("embeddings"), list):
                raw = payload["embeddings"]
            elif isinstance(payload.get("embedding"), list):
                raw = [payload["embedding"]]
            else:
                raise KnowledgeUnavailable("embedding response has no vectors")
        elif isinstance(payload, list):
            raw = payload
        else:
            raise KnowledgeUnavailable("embedding response has an unsupported shape")
        vectors: list[list[float]] = []
        for item in raw:
            if not isinstance(item, list):
                raise KnowledgeUnavailable("embedding response contains a non-vector item")
            try:
                vectors.append([float(value) for value in item])
            except (TypeError, ValueError) as exc:
                raise KnowledgeUnavailable("embedding vector contains a non-number") from exc
        return vectors

    async def aclose(self) -> None:
        if not self._owns_client:
            return
        clients = list(self._loop_clients.values())
        self._loop_clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - closing a foreign loop's client may fail
                logger.debug("embedding client close failed", exc_info=True)
