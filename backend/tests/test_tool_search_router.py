import pytest

from agentkit.config.contextforge_config import ToolSearchConfig
from agentkit.contextforge.catalog import ToolCatalogEntry
from agentkit.contextforge.index import ToolVectorHit
from agentkit.tools.search.service import ToolSearchService


def _entry(name: str, description: str, owner: str | None = None) -> ToolCatalogEntry:
    content = f"{name}\n{name}\n{description}"
    return ToolCatalogEntry(
        name=name,
        display_name=name,
        description=description,
        tags=(),
        parameters=("query",),
        server="contextforge",
        content=content,
        content_hash=name + "-hash",
        updated_at=1,
        owner=owner,
    )


class FakeIndex:
    def __init__(self):
        self.fail = False

    async def hybrid_search(self, *, vector, query, limit):
        if self.fail:
            raise RuntimeError("milvus down")
        return [
            ToolVectorHit("github_search", 0.9),
            ToolVectorHit("gitlab_search", 0.7),
            ToolVectorHit("stale_tool", 0.8),
        ]


class FakeEmbedder:
    async def embed_query(self, text):
        return [0.0] * 8


class FakeReranker:
    async def rerank(self, *, query, documents):
        return [type("Score", (), {"index": 1}), type("Score", (), {"index": 0})]


@pytest.mark.asyncio
async def test_search_confirms_hits_and_reranks() -> None:
    entries = (_entry("github_search", "search github code"), _entry("gitlab_search", "search gitlab code"))
    service = ToolSearchService(
        index=FakeIndex(),
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=2,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )
    outcome = await service.search(task_query="github code")
    assert [hit.entry.name for hit in outcome.hits] == ["gitlab_search", "github_search"]
    assert outcome.reranked is True
    assert "stale_tool" not in [hit.entry.name for hit in outcome.hits]


@pytest.mark.asyncio
async def test_vector_failure_falls_back_to_local_bm25() -> None:
    entries = (_entry("github_search", "search github code"),)
    index = FakeIndex()
    index.fail = True
    service = ToolSearchService(
        index=index,
        embedder=FakeEmbedder(),
        reranker=None,
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=2,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )
    outcome = await service.search(task_query="github")
    assert outcome.index_kind == "local_bm25"
    assert outcome.hits[0].entry.name == "github_search"
    assert "vector_unavailable" in outcome.warnings


@pytest.mark.asyncio
async def test_empty_query_returns_empty() -> None:
    service = ToolSearchService(
        index=None,
        embedder=None,
        reranker=None,
        config=ToolSearchConfig(embedding={"enabled": False}),
        snapshot_provider=lambda: (),
    )
    assert (await service.search(task_query=" ")).hits == ()

@pytest.mark.asyncio
async def test_search_only_confirms_the_callers_own_tools() -> None:
    entries = (
        _entry("github_search", "search github code", "u1"),
        _entry("gitlab_search", "search gitlab code", "u2"),
    )
    service = ToolSearchService(
        index=FakeIndex(),
        embedder=FakeEmbedder(),
        reranker=None,
        config=ToolSearchConfig(
            candidate_k=5,
            top_k=5,
            embedding={"base_url": "http://embed", "dimensions": 8},
        ),
        snapshot_provider=lambda: entries,
    )

    outcome = await service.search(task_query="github gitlab", user_id="u1")

    assert [hit.entry.name for hit in outcome.hits] == ["github_search"]
