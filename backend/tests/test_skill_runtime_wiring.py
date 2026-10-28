"""Task 15: app lifespan builds, starts, and shuts down the SkillRuntime.

The full lifespan is exercised with the runtime factory patched to a fake, so
these tests cover the wiring (ordering, app.state, process registry, outbox and
projection lifetime) without PostgreSQL, Git, or Milvus.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentkit.config.app_config import AppConfig
from agentkit.config.auth_config import AuthConfig
from agentkit.config.skills_config import SkillSearchConfig
from agentkit.knowledge.config import MilvusConfig
from agentkit.skills import search as skill_search
from agentkit.skills.catalog import SkillSnapshot, get_skill_snapshot, set_skill_snapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.git import GitCommandError
from agentkit.skills.projection import MilvusSkillProjection, ProjectionRow
from agentkit.skills.search import (
    LEXICAL_FALLBACK,
    VECTOR_UNAVAILABLE,
    SkillSearchService,
)
from agentkit.skills.types import Skill, SkillCategory
from pydantic import ValidationError
from backend.app.gateway import app as gateway_app
from backend.app.gateway import skills_catalog
from backend.app.gateway.skills_catalog import (
    SkillRuntime,
    build_skill_runtime,
    configure_skill_runtime,
    get_skill_runtime,
    reset_skill_runtime,
)

GIT_SKILLS = {
    "source": "git",
    "git": {"repository": "https://example.test/skills.git"},
}


def _config(**skills: Any) -> AppConfig:
    return AppConfig(auth=AuthConfig(enabled=False), skills=skills or GIT_SKILLS)


@dataclass
class FakeGit:
    ready: bool = False
    order: list[str] | None = None

    async def ensure_ready(self) -> None:
        self.ready = True
        if self.order is not None:
            self.order.append("git.ensure_ready")


@dataclass
class FakeOutbox:
    order: list[str] = field(default_factory=list)
    started: bool = False
    stopped: bool = False

    async def start(self) -> None:
        self.started = True
        self.order.append("outbox.start")

    async def stop(self) -> None:
        self.stopped = True
        self.order.append("outbox.stop")


@dataclass
class FakeProjection:
    order: list[str] = field(default_factory=list)
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True
        self.order.append("projection.aclose")


@dataclass
class FakeCatalog:
    order: list[str] = field(default_factory=list)
    snapshot: SkillSnapshot = field(
        default_factory=lambda: SkillSnapshot(epoch="built", public=(), private_by_owner={})
    )

    async def refresh(self) -> SkillSnapshot:
        self.order.append("catalog.refresh")
        set_skill_snapshot(self.snapshot)
        return self.snapshot


@dataclass
class FakeStore:
    rows: list[Any] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    async def all_active(self) -> list[Any]:
        self.order.append("store.all_active")
        return list(self.rows)


@dataclass
class FakeContent:
    order: list[str] = field(default_factory=list)
    public: list[list[Any]] = field(default_factory=list)
    private: list[tuple[str, list[Any]]] = field(default_factory=list)
    existing_owners: list[str] = field(default_factory=list)

    async def materialize_public(self, active: list[Any]) -> Path:
        self.order.append("content.materialize_public")
        self.public.append(list(active))
        return Path("active/public")

    async def materialize_private(self, owner: str, active: list[Any]) -> Path:
        self.order.append(f"content.materialize_private:{owner}")
        self.private.append((owner, list(active)))
        return Path("active/users") / owner

    def existing_private_owners(self) -> list[str]:
        return list(self.existing_owners)


def _row(scope: SkillScope, owner: str | None) -> SimpleNamespace:
    return SimpleNamespace(scope=scope, skill=SimpleNamespace(owner_user_id=owner))


@dataclass
class FakeRuntime:
    """Duck-typed runtime: lifespan only calls start()/stop() on it."""

    order: list[str] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    author: Any = field(default_factory=lambda: SimpleNamespace(name="author"))
    review: Any = field(default_factory=lambda: SimpleNamespace(name="review"))
    catalog: Any = field(default_factory=lambda: SimpleNamespace(name="catalog"))
    search: Any = field(default_factory=lambda: SimpleNamespace(name="search"))

    async def start(self) -> None:
        self.started = True
        self.order.append("runtime.start")

    async def stop(self) -> None:
        self.stopped = True
        self.order.append("runtime.stop")


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_skill_runtime()
    yield
    reset_skill_runtime()
    skill_search.reset_skill_runtime()


# ---------------------------------------------------------------------------
# runtime refresh: snapshot first, then the active mount trees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_publishes_the_snapshot_then_materializes_every_tree() -> None:
    order: list[str] = []
    catalog = FakeCatalog(order=order)
    store = FakeStore(
        order=order,
        rows=[
            _row(SkillScope.PUBLIC, "author-id"),
            _row(SkillScope.PRIVATE, "alice"),
            _row(SkillScope.PRIVATE, "bob"),
        ],
    )
    content = FakeContent(order=order)
    runtime = SkillRuntime(
        store=store,
        git=FakeGit(),
        content=content,
        author=None,
        review=None,
        catalog=catalog,
    )

    snapshot = await runtime.refresh()

    assert snapshot is catalog.snapshot
    assert get_skill_snapshot() is catalog.snapshot
    assert content.public == [[store.rows[0]]]
    assert sorted(owner for owner, _rows in content.private) == ["alice", "bob"]
    assert {owner: [row.skill.owner_user_id for row in rows] for owner, rows in content.private} == {
        "alice": ["alice"],
        "bob": ["bob"],
    }
    assert order == [
        "catalog.refresh",
        "store.all_active",
        "content.materialize_public",
        "content.materialize_private:alice",
        "content.materialize_private:bob",
    ]


@pytest.mark.asyncio
async def test_refresh_still_rebuilds_the_public_tree_when_it_is_empty() -> None:
    content = FakeContent()
    runtime = SkillRuntime(
        store=FakeStore(rows=[]),
        git=FakeGit(),
        content=content,
        author=None,
        review=None,
        catalog=FakeCatalog(),
    )

    await runtime.refresh()

    assert content.public == [[]]
    assert content.private == []


# ---------------------------------------------------------------------------
# runtime start/stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_readies_git_refreshes_and_starts_the_outbox() -> None:
    order: list[str] = []
    git = FakeGit(order=order)
    outbox = FakeOutbox(order=order)
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=git,
        content=FakeContent(order=order),
        author=None,
        review=None,
        search=None,
        catalog=FakeCatalog(order=order),
        outbox=outbox,
    )

    await runtime.start()

    assert git.ready is True
    assert outbox.started is True
    assert order == [
        "git.ensure_ready",
        "catalog.refresh",
        "store.all_active",
        "content.materialize_public",
        "outbox.start",
    ]


@pytest.mark.asyncio
async def test_stop_stops_the_outbox_closes_the_projection_and_resets_process_state() -> None:
    order: list[str] = []
    outbox = FakeOutbox(order=order)
    projection = FakeProjection(order=order)
    runtime = SkillRuntime(
        store=FakeStore(),
        git=FakeGit(),
        content=FakeContent(),
        author=None,
        review=None,
        catalog=FakeCatalog(),
        outbox=outbox,
        projection=projection,
    )
    configure_skill_runtime(runtime)
    set_skill_snapshot(SkillSnapshot(epoch="live", public=(), private_by_owner={}))

    await runtime.stop()

    assert outbox.stopped is True
    assert projection.closed is True
    assert order == ["outbox.stop", "projection.aclose"]
    assert get_skill_runtime() is None
    assert skill_search.get_skill_runtime() is None
    assert get_skill_snapshot().epoch == ""


# ---------------------------------------------------------------------------
# app lifespan
# ---------------------------------------------------------------------------


def test_lifespan_builds_starts_and_stops_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRuntime()
    built: list[AppConfig] = []

    def build(config: AppConfig) -> FakeRuntime:
        built.append(config)
        return fake

    async def recover(run_store, thread_meta, *, gateway_workers=1) -> int:
        return 0

    monkeypatch.setattr(gateway_app, "get_app_config", _config)
    monkeypatch.setattr(gateway_app, "build_skill_runtime", build)
    monkeypatch.setattr(gateway_app, "recover_inflight_runs", recover)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with gateway_app.lifespan(app):
            assert built and built[0].skills.source == "git"
            assert fake.started is True and fake.stopped is False
            assert app.state.skill_runtime is fake
            assert app.state.skill_author_service is fake.author
            assert app.state.skill_review_service is fake.review
            assert get_skill_runtime() is fake
            assert skill_search.get_skill_runtime() is fake.search
        assert fake.stopped is True
        assert app.state.skill_runtime is None
        assert app.state.skill_author_service is None
        assert app.state.skill_review_service is None
        assert get_skill_runtime() is None
        assert get_skill_snapshot().epoch == ""

    asyncio.run(scenario())


def test_filesystem_source_never_builds_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace(skill_runtime=object()))
        runtime = await gateway_app.start_skill_runtime(app, AppConfig(auth=AuthConfig(enabled=False)))
        assert runtime is None
        assert app.state.skill_runtime is None
        await gateway_app.stop_skill_runtime(app)

    def build(config: AppConfig) -> FakeRuntime:
        raise AssertionError("filesystem mode must not build a git runtime")

    monkeypatch.setattr(gateway_app, "build_skill_runtime", build)
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# runtime factory: projection/outbox/search are optional
# ---------------------------------------------------------------------------


@dataclass
class _Constructed:
    calls: list[tuple[str, Any]] = field(default_factory=list)


def _patch_factory(monkeypatch: pytest.MonkeyPatch, *, embedding: bool) -> _Constructed:
    seen = _Constructed()

    class Store:
        def __init__(self, session_factory: Any) -> None:
            seen.calls.append(("store", session_factory))

    class Git:
        def __init__(self, config: Any, cache_dir: Path) -> None:
            seen.calls.append(("git", (config, cache_dir)))

    class Content:
        def __init__(self, cache_dir: Path, git: Any) -> None:
            seen.calls.append(("content", (cache_dir, git)))

    class Author:
        def __init__(
            self,
            store: Any,
            git: Any,
            content: Any,
            creation: Any,
            *,
            reserved_names: Any = None,
        ) -> None:
            seen.calls.append(("author", (creation, reserved_names)))

    class Review:
        def __init__(
            self,
            store: Any,
            git: Any,
            content: Any,
            review: Any,
            *,
            reserved_names: Any = None,
        ) -> None:
            seen.calls.append(("review", (review, reserved_names)))

    class Catalog:
        def __init__(self, store: Any, content: Any, container_path: str) -> None:
            seen.calls.append(("catalog", None))

    class Embedder:
        def __init__(self, config: Any) -> None:
            seen.calls.append(("embedder", config))

    class Projection:
        def __init__(self, config: Any, *, dimensions: int) -> None:
            seen.calls.append(("projection", (config, dimensions)))

    class Search:
        def __init__(
            self,
            store: Any,
            projection: Any,
            content: Any,
            embedder: Any,
            config: Any,
            reranker: Any = None,
        ) -> None:
            seen.calls.append(("search", (projection, embedder, config, reranker)))

    class Outbox:
        def __init__(
            self, store: Any, content: Any, embedder: Any, projection: Any, *, poll_seconds: float
        ) -> None:
            seen.calls.append(("outbox", poll_seconds))

    monkeypatch.setattr(skills_catalog, "SqlSkillStore", Store)
    monkeypatch.setattr(skills_catalog, "SkillGitRepository", Git)
    monkeypatch.setattr(skills_catalog, "SkillContentStore", Content)
    monkeypatch.setattr(skills_catalog, "SkillAuthorService", Author)
    monkeypatch.setattr(skills_catalog, "SkillReviewService", Review)
    monkeypatch.setattr(skills_catalog, "SkillCatalogService", Catalog)
    monkeypatch.setattr(skills_catalog, "OpenAICompatibleEmbedder", Embedder)
    monkeypatch.setattr(skills_catalog, "MilvusSkillProjection", Projection)
    monkeypatch.setattr(skills_catalog, "SkillSearchService", Search)
    monkeypatch.setattr(skills_catalog, "SkillIndexOutboxWorker", Outbox)
    del embedding
    return seen


def test_factory_injects_builtin_names_for_author_and_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_factory(monkeypatch, embedding=False)
    monkeypatch.setattr(
        skills_catalog,
        "builtin_names",
        lambda config: frozenset({"demo"}),
    )

    build_skill_runtime(_config(**GIT_SKILLS), session_factory=object())

    payloads = {name: payload for name, payload in seen.calls}
    author_reserved = payloads["author"][1]
    review_reserved = payloads["review"][1]
    assert author_reserved is review_reserved
    assert author_reserved() == frozenset({"demo"})


def test_shipped_git_config_still_builds_the_lexical_search_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped config is search.enabled=true + embedding.enabled=false.

    Git mode must still wire the search service: the documented local BM25 path
    is the only retrieval channel there, so a missing embedding endpoint may drop
    the projection and the outbox but never the service itself.
    """
    seen = _patch_factory(monkeypatch, embedding=False)
    config = _config(**GIT_SKILLS)
    assert config.skills.search.enabled is True
    assert config.skills.search.embedding.enabled is False

    runtime = build_skill_runtime(config, session_factory=object())

    assert runtime.store is not None
    assert runtime.author is not None and runtime.review is not None
    assert runtime.catalog is not None
    # Lexical-only: the service exists, the Milvus projection and its outbox do not.
    assert runtime.search is not None
    assert runtime.projection is None
    assert runtime.outbox is None
    # reranker 也必须是 None：shipped 配置里 rerank.enabled=false。
    assert ("search", (None, None, config.skills.search, None)) in seen.calls
    assert [name for name, _ in seen.calls] == [
        "store",
        "git",
        "content",
        "author",
        "review",
        "catalog",
        "search",
    ]


@pytest.mark.asyncio
async def test_keyword_search_answers_without_milvus_or_an_embedder() -> None:
    """No projection, no embedder: the same query is answered by local BM25."""
    service = SkillSearchService(
        store=_LexicalStore(),
        projection=None,
        content=_LexicalContent(),
        embedder=None,
        config=SkillSearchConfig(),
    )

    outcome = await service.search(
        user_id="alice",
        task_query="pdf report",
        domain_keywords=(),
        available_skills=None,
        prefs=None,
    )

    assert [hit.skill.name for hit in outcome.hits] == ["pdf-report"]
    assert outcome.warnings == (VECTOR_UNAVAILABLE, LEXICAL_FALLBACK)


def test_factory_builds_projection_outbox_and_search_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_factory(monkeypatch, embedding=True)
    skills = {
        "source": "git",
        "git": {"repository": "https://example.test/skills.git"},
        "search": {
            "embedding": {
                "enabled": True,
                "base_url": "http://embed.test/v1",
                "dimensions": 8,
                "model": "fake-embedding",
            },
            "vector_store": {"uri": "http://skills-milvus:19530"},
        },
        "index": {"outbox_poll_seconds": 0.5},
    }
    config = _config(**skills)

    runtime = build_skill_runtime(config, session_factory=object())

    assert runtime.projection is not None
    assert runtime.outbox is not None
    assert runtime.search is not None
    # The projection gets its own vector-store config, never the knowledge one.
    assert ("projection", (config.skills.search.vector_store, 8)) in seen.calls
    assert ("outbox", 0.5) in seen.calls
    assert any(name == "search" for name, _ in seen.calls)


# ---------------------------------------------------------------------------
# the projection targets the skills active collection, never the knowledge one
# ---------------------------------------------------------------------------

_EMBEDDING = {
    "enabled": True,
    "base_url": "http://embed.test/v1",
    "dimensions": 8,
    "model": "fake-embedding",
}

_EMBEDDING_SKILLS = {
    "source": "git",
    "git": {"repository": "https://example.test/skills.git"},
    "search": {"embedding": dict(_EMBEDDING)},
}

# The real deployment spells an explicit collection here (config.yaml does).
_KNOWLEDGE_VECTOR_STORE = {
    "uri": "http://knowledge-milvus:19530",
    "collection": "myflow_knowledge",
}


class FakeMilvusClient:
    """Records which collection every projection write/read targets."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.searches: list[dict] = []

    def has_collection(self, name: str) -> bool:
        return True

    def load_collection(self, name: str) -> None:
        return None

    def describe_collection(self, name: str) -> dict:
        return {"fields": []}

    def upsert(self, collection_name: str, data: list[dict]) -> dict:
        self.upserts.append({"collection_name": collection_name, "data": data})
        return {"upsert_count": len(data)}

    def search(self, collection_name: str, **kwargs: Any) -> list[list[dict]]:
        self.searches.append({"collection_name": collection_name, **kwargs})
        return [[]]


def _captured_projection(seen: _Constructed) -> tuple[Any, int]:
    payloads = [payload for name, payload in seen.calls if name == "projection"]
    assert len(payloads) == 1, f"expected exactly one projection build, got {payloads}"
    return payloads[0]


def _projection_row(dimensions: int) -> ProjectionRow:
    return ProjectionRow(
        projection_key="public:s1",
        scope="public",
        owner_user_id=None,
        skill_id="s1",
        version_id="v1",
        skill_version="1.0.0",
        enabled=True,
        name="public skill",
        description="public description",
        package_sha256="p1",
        skill_md_sha256="m1",
        embedding=[0.1] * dimensions,
    )


def test_skills_search_vector_store_defaults_to_the_active_collection() -> None:
    assert SkillSearchConfig().vector_store.collection == "myflow_skills_active"


@pytest.mark.asyncio
async def test_projection_targets_the_skills_active_collection_not_the_knowledge_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_factory(monkeypatch, embedding=True)
    config = AppConfig(
        auth=AuthConfig(enabled=False),
        skills=_EMBEDDING_SKILLS,
        knowledge_base={"vector_store": dict(_KNOWLEDGE_VECTOR_STORE)},
    )

    build_skill_runtime(config, session_factory=object())

    projection_config, dimensions = _captured_projection(seen)
    # Connection parameters fall back to the knowledge Milvus; the collection does not.
    assert projection_config.uri == "http://knowledge-milvus:19530"
    assert projection_config.collection == "myflow_skills_active"

    client = FakeMilvusClient()
    projection = MilvusSkillProjection(projection_config, dimensions=dimensions, client=client)
    await projection.upsert_active(_projection_row(dimensions))
    await projection.search(
        user_id="alice", private_skill_ids=(), vector=[0.1] * dimensions, limit=3
    )

    assert [call["collection_name"] for call in client.upserts] == ["myflow_skills_active"]
    assert [call["collection_name"] for call in client.searches] == ["myflow_skills_active"]


@pytest.mark.asyncio
async def test_skills_vector_store_uri_overrides_the_knowledge_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_factory(monkeypatch, embedding=True)
    skills = {
        "source": "git",
        "git": {"repository": "https://example.test/skills.git"},
        "search": {
            "embedding": dict(_EMBEDDING),
            "vector_store": {"uri": "http://skills-milvus:19530"},
        },
    }
    config = AppConfig(
        auth=AuthConfig(enabled=False),
        skills=skills,
        knowledge_base={"vector_store": dict(_KNOWLEDGE_VECTOR_STORE)},
    )

    build_skill_runtime(config, session_factory=object())

    projection_config, dimensions = _captured_projection(seen)
    assert projection_config.uri == "http://skills-milvus:19530"
    assert projection_config.collection == "myflow_skills_active"

    client = FakeMilvusClient()
    projection = MilvusSkillProjection(projection_config, dimensions=dimensions, client=client)
    await projection.upsert_active(_projection_row(dimensions))

    assert [call["collection_name"] for call in client.upserts] == ["myflow_skills_active"]


# ---------------------------------------------------------------------------
# startup tolerates a Git outage but not a broken catalog
# ---------------------------------------------------------------------------


@dataclass
class FailingGit:
    """Remote Git is unreachable: ensure_ready fails, the cache is still readable."""

    order: list[str]
    message: str = "fatal: unable to access 'https://example.test/skills.git'"

    async def ensure_ready(self) -> None:
        self.order.append("git.ensure_ready")
        raise GitCommandError(("git", "clone", "--mirror"), 128, self.message)


@pytest.mark.asyncio
async def test_start_survives_a_git_outage_and_serves_the_local_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    catalog = FakeCatalog(order=order)
    outbox = FakeOutbox(order=order)
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FailingGit(order),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=catalog,
        outbox=outbox,
    )

    with caplog.at_level(logging.WARNING, logger=skills_catalog.__name__):
        await runtime.start()

    assert order == [
        "git.ensure_ready",
        "catalog.refresh",
        "store.all_active",
        "content.materialize_public",
        "outbox.start",
    ]
    assert outbox.started is True
    assert get_skill_snapshot() is catalog.snapshot
    messages = [record.getMessage() for record in caplog.records]
    assert any("unable to access" in message for message in messages), messages
    # The outage is not hidden from the write paths: the warning says they still fail.
    assert any(
        "Saving, publishing, and cache rebuilds will keep failing" in message
        for message in messages
    ), messages


@pytest.mark.asyncio
async def test_start_accepts_an_empty_snapshot_when_the_cache_has_nothing() -> None:
    order: list[str] = []
    empty = SkillSnapshot(epoch="empty", public=(), private_by_owner={})
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FailingGit(order),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=FakeCatalog(order=order, snapshot=empty),
        outbox=FakeOutbox(order=order),
    )

    await runtime.start()

    assert get_skill_snapshot() is empty
    assert get_skill_snapshot().public == ()


@pytest.mark.asyncio
async def test_start_still_fails_when_the_catalog_cannot_be_read() -> None:
    class BrokenCatalog:
        async def refresh(self) -> Any:
            raise RuntimeError("postgres is down")

    runtime = SkillRuntime(
        store=FakeStore(),
        git=FakeGit(),
        content=FakeContent(),
        author=None,
        review=None,
        catalog=BrokenCatalog(),
        outbox=FakeOutbox(),
    )

    with pytest.raises(RuntimeError, match="postgres is down"):
        await runtime.start()


# ---------------------------------------------------------------------------
# index maintenance: reconciliation_interval_minutes actually schedules work
# ---------------------------------------------------------------------------


@dataclass
class RecordingOutbox(FakeOutbox):
    """Records reconcile/prune so the sweeper can be asserted without Milvus."""

    reconciled: int = 0
    pruned: int = 0
    keep: int | None = None
    min_age_seconds: float | None = None
    fail_with: Exception | None = None

    async def reconcile(self, *, force: bool = False) -> int:
        self.order.append("outbox.reconcile")
        if self.fail_with is not None:
            raise self.fail_with
        return self.reconciled

    async def prune(
        self, keep_versions_per_skill: int = 20, *, min_age_seconds: float = 0.0
    ) -> int:
        self.order.append("outbox.prune")
        self.keep = keep_versions_per_skill
        self.min_age_seconds = min_age_seconds
        return self.pruned


@pytest.mark.asyncio
async def test_one_sweep_reconciles_then_prunes_with_the_configured_keep() -> None:
    order: list[str] = []
    outbox = RecordingOutbox(order=order, reconciled=2, pruned=3)
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FakeGit(),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=FakeCatalog(order=order),
        outbox=outbox,
        keep_versions_per_skill=7,
        prune_min_age_seconds=86400.0,
    )

    assert await runtime.reconcile_projection_once() == (2, 3)
    assert order == ["outbox.reconcile", "outbox.prune"]
    assert outbox.keep == 7
    # retention.cache_ttl_days 是分钟/秒换算后的安全期，必须真的传下去。
    assert outbox.min_age_seconds == 86400.0


@pytest.mark.asyncio
async def test_a_runtime_without_a_projection_has_nothing_to_reconcile() -> None:
    runtime = SkillRuntime(
        store=FakeStore(),
        git=FakeGit(),
        content=FakeContent(),
        author=None,
        review=None,
        outbox=None,
    )

    assert await runtime.reconcile_projection_once() == (0, 0)


@pytest.mark.asyncio
async def test_index_maintenance_is_scheduled_and_stopped_with_the_runtime() -> None:
    order: list[str] = []
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FakeGit(),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=FakeCatalog(order=order),
        outbox=FakeOutbox(order=order),
        reconcile_interval_seconds=3600.0,
    )

    await runtime.start()
    task = runtime._index_maintenance_task  # noqa: SLF001 - lifecycle assertion
    assert task is not None
    assert task.get_name() == "skill-index-maintenance"

    await runtime.stop()
    assert runtime._index_maintenance_task is None  # noqa: SLF001
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_index_maintenance_needs_a_projection_to_schedule() -> None:
    order: list[str] = []
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FakeGit(),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=FakeCatalog(order=order),
        outbox=None,
        reconcile_interval_seconds=60.0,
    )

    await runtime.start()

    assert runtime._index_maintenance_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_failing_sweep_logs_and_keeps_the_loop_alive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    outbox = RecordingOutbox(order=order, fail_with=RuntimeError("milvus is down"))
    runtime = SkillRuntime(
        store=FakeStore(order=order),
        git=FakeGit(),
        content=FakeContent(order=order),
        author=None,
        review=None,
        catalog=FakeCatalog(order=order),
        outbox=outbox,
        reconcile_interval_seconds=0.01,
    )

    with caplog.at_level(logging.WARNING, logger=skills_catalog.__name__):
        await runtime.start()
        task = runtime._index_maintenance_task  # noqa: SLF001
        assert task is not None
        await asyncio.sleep(0.05)
        assert not task.done(), "a failed sweep must not kill the loop"
        await runtime.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Skill index maintenance failed" in message for message in messages), messages
    assert order.count("outbox.reconcile") >= 2


# ---------------------------------------------------------------------------
# a runtime that fails to start is torn down, not left half-installed
# ---------------------------------------------------------------------------


@dataclass
class FailingStartRuntime(FakeRuntime):
    async def start(self) -> None:
        self.order.append("runtime.start")
        raise RuntimeError("outbox worker refused to start")


def test_a_failed_runtime_start_stops_it_and_clears_app_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FailingStartRuntime()
    monkeypatch.setattr(gateway_app, "build_skill_runtime", lambda config: fake)
    config = AppConfig(auth=AuthConfig(enabled=False), skills=GIT_SKILLS)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(RuntimeError, match="refused to start"):
            await gateway_app.start_skill_runtime(app, config)
        assert fake.order == ["runtime.start", "runtime.stop"]
        assert fake.stopped is True
        assert app.state.skill_runtime is None
        assert app.state.skill_author_service is None
        assert app.state.skill_review_service is None
        assert app.state.skill_catalog is None
        assert app.state.skill_search is None
        assert get_skill_runtime() is None
        assert skill_search.get_skill_runtime() is None
        assert get_skill_snapshot().epoch == ""

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# git catalog mode needs a SQL database
# ---------------------------------------------------------------------------


def test_git_catalog_mode_requires_a_sql_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills_catalog, "get_session_factory", lambda: None)
    config = AppConfig(
        auth=AuthConfig(enabled=False),
        skills=GIT_SKILLS,
        database={"backend": "memory"},
    )

    with pytest.raises(RuntimeError) as excinfo:
        build_skill_runtime(config)

    message = str(excinfo.value)
    assert "SQL database" in message
    assert "skills.source=git" in message
    assert "database.backend='memory'" in message


def test_git_catalog_mode_requires_a_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """空仓库地址不是"暂时连不上"：必须在启动前就炸，而不是健康地跑着空目录。"""

    monkeypatch.setattr(skills_catalog, "get_session_factory", lambda: object())
    config = AppConfig(
        auth=AuthConfig(enabled=False),
        skills={"source": "git"},
    )

    with pytest.raises(RuntimeError) as excinfo:
        build_skill_runtime(config)

    message = str(excinfo.value)
    assert "skills.git.repository" in message
    assert "SKILLS_GIT_REPOSITORY" in message


# ---------------------------------------------------------------------------
# lexical retrieval really answers without Milvus (Fix 1)
# ---------------------------------------------------------------------------


def _lexical_visible(name: str = "pdf-report") -> Any:
    skill = Skill(
        name=name,
        description="pdf report generator",
        license=None,
        skill_dir=Path("cache") / "s1",
        skill_file=Path("cache") / "s1" / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        skill_id="s1",
        scope=SkillScope.PRIVATE,
        owner_user_id="alice",
        version_id="version-s1",
    )
    return SimpleNamespace(
        skill=skill,
        scope=SkillScope.PRIVATE,
        version=SimpleNamespace(
            skill_id="s1",
            id="version-s1",
            draft_commit="a" * 40,
            skill_path="users/alice/s1/SKILL.md",
        ),
        release=None,
    )


class _LexicalStore:
    """Only the three reads SkillSearchService performs on the catalog."""

    async def private_skill_ids(self, user_id: str) -> tuple[str, ...]:
        return ("s1",)

    async def visible_for_user(self, user_id: str) -> list[Any]:
        return [_lexical_visible()]


class _LexicalContent:
    async def read_text(self, commit: str, path: str) -> str:
        return (
            "---\nname: pdf-report\ndescription: pdf report generator\n---\n"
            "Build the pdf report from the template.\n"
        )


# ---------------------------------------------------------------------------
# the active collection is pinned and the connection knobs survive (Fix 2)
# ---------------------------------------------------------------------------


def test_skills_vector_store_rejects_a_foreign_collection() -> None:
    """The projection collection is a retrieval contract, not a deployment knob."""
    with pytest.raises(ValidationError) as excinfo:
        SkillSearchConfig(vector_store={"collection": "myflow_knowledge"})

    assert "myflow_skills_active" in str(excinfo.value)


def test_borrowing_the_knowledge_connection_keeps_the_tuning_knobs() -> None:
    config = SkillSearchConfig(
        vector_store={
            "consistency_level": "Bounded",
            "metric_type": "L2",
            "index_type": "IVF_FLAT",
            "flush": False,
            "timeout_seconds": 7.5,
        }
    )

    effective = config.active_vector_store(
        MilvusConfig(uri="http://knowledge-milvus:19530", token="secret", db_name="kb")
    )

    assert effective.uri == "http://knowledge-milvus:19530"
    assert effective.token == "secret"
    assert effective.db_name == "kb"
    assert effective.collection == "myflow_skills_active"
    assert effective.consistency_level == "Bounded"
    assert effective.metric_type == "L2"
    assert effective.index_type == "IVF_FLAT"
    assert effective.flush is False
    assert effective.timeout_seconds == 7.5


def test_embeddings_without_any_milvus_endpoint_fail_at_build_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No skills endpoint and nothing to borrow: fail loudly, not at first upsert."""
    _patch_factory(monkeypatch, embedding=True)
    config = AppConfig(auth=AuthConfig(enabled=False), skills=_EMBEDDING_SKILLS)

    with pytest.raises(RuntimeError) as excinfo:
        build_skill_runtime(config, session_factory=object())

    message = str(excinfo.value)
    assert "skills.search.vector_store.uri" in message
    assert "knowledge_base.vector_store.uri" in message


# ---------------------------------------------------------------------------
# private mount trees of owners who lost their last skill are pruned (Fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_prunes_private_trees_of_owners_without_private_skills() -> None:
    content = FakeContent(existing_owners=["alice", "carol"])
    store = FakeStore(rows=[_row(SkillScope.PRIVATE, "alice")])
    runtime = SkillRuntime(
        store=store,
        git=FakeGit(),
        content=content,
        author=None,
        review=None,
        catalog=FakeCatalog(),
    )

    await runtime.refresh()

    assert [owner for owner, _rows in content.private] == ["alice", "carol"]
    assert content.private[0][1] == [store.rows[0]]
    # An owner with no remaining active rows gets an empty tree written over the old one.
    assert content.private[1] == ("carol", [])


@pytest.mark.asyncio
async def test_a_broken_private_tree_does_not_stop_the_other_prunes() -> None:
    class HalfBrokenContent(FakeContent):
        async def materialize_private(self, owner: str, active: list[Any]) -> Path:
            await super().materialize_private(owner, active)
            if owner == "alice":
                raise RuntimeError("disk full")
            return Path("active/users") / owner

    content = HalfBrokenContent(existing_owners=["alice", "carol", "dave"])
    runtime = SkillRuntime(
        store=FakeStore(rows=[]),
        git=FakeGit(),
        content=content,
        author=None,
        review=None,
        catalog=FakeCatalog(),
    )

    await runtime.refresh()

    assert [owner for owner, _rows in content.private] == ["alice", "carol", "dave"]


# ---------------------------------------------------------------------------
# a later startup failure still tears the runtime down (Fix 4)
# ---------------------------------------------------------------------------


def test_a_failed_later_startup_step_tears_the_runtime_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(gateway_app, "get_app_config", _config)
    monkeypatch.setattr(gateway_app, "build_skill_runtime", lambda config: fake)

    def boom(config: Any) -> Any:
        raise RuntimeError("subagent runtime exploded")

    monkeypatch.setattr(gateway_app, "get_subagent_runtime", boom)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(RuntimeError, match="exploded"):
            async with gateway_app.lifespan(app):
                pytest.fail("lifespan must not reach its yield")
        assert fake.started is True
        assert fake.stopped is True
        assert fake.order == ["runtime.start", "runtime.stop"]
        assert app.state.skill_runtime is None
        assert app.state.skill_author_service is None
        assert app.state.skill_review_service is None
        assert app.state.skill_catalog is None
        assert app.state.skill_search is None
        assert get_skill_runtime() is None
        assert skill_search.get_skill_runtime() is None

    asyncio.run(scenario())


def test_a_failed_knowledge_service_start_tears_the_runtime_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(gateway_app, "get_app_config", _config)
    monkeypatch.setattr(gateway_app, "build_skill_runtime", lambda config: fake)

    async def boom(config: Any) -> Any:
        raise RuntimeError("knowledge metadata is unreachable")

    monkeypatch.setattr(gateway_app, "configure_knowledge_service", boom)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(RuntimeError, match="unreachable"):
            async with gateway_app.lifespan(app):
                pytest.fail("lifespan must not reach its yield")
        assert fake.stopped is True
        assert app.state.skill_runtime is None
        assert get_skill_runtime() is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# graceful shutdown drains runs before it stops the skill runtime (Fix 5)
# ---------------------------------------------------------------------------


def test_shutdown_drains_the_run_manager_before_stopping_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    fake = FakeRuntime(order=order)
    monkeypatch.setattr(gateway_app, "get_app_config", _config)
    monkeypatch.setattr(gateway_app, "build_skill_runtime", lambda config: fake)

    async def shutdown(timeout: float) -> None:
        order.append("run_manager.shutdown")

    class Bridge:
        async def aclose(self) -> None:
            order.append("bridge.aclose")

    services = SimpleNamespace(
        run_manager=SimpleNamespace(shutdown=shutdown), bridge=Bridge()
    )
    monkeypatch.setattr(gateway_app, "build_run_services", lambda *a, **k: services)

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        async with gateway_app.lifespan(app):
            pass

    asyncio.run(scenario())

    assert "run_manager.shutdown" in order and "runtime.stop" in order
    assert order.index("run_manager.shutdown") < order.index("runtime.stop")
