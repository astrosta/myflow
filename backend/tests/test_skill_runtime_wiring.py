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
from agentkit.skills import search as skill_search
from agentkit.skills.catalog import SkillSnapshot, get_skill_snapshot, set_skill_snapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.git import GitCommandError
from agentkit.skills.projection import MilvusSkillProjection, ProjectionRow
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

    async def materialize_public(self, active: list[Any]) -> Path:
        self.order.append("content.materialize_public")
        self.public.append(list(active))
        return Path("active/public")

    async def materialize_private(self, owner: str, active: list[Any]) -> Path:
        self.order.append(f"content.materialize_private:{owner}")
        self.private.append((owner, list(active)))
        return Path("active/users") / owner


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
        def __init__(self, store: Any, git: Any, content: Any, creation: Any) -> None:
            seen.calls.append(("author", (creation,)))

    class Review:
        def __init__(self, store: Any, git: Any, content: Any, review: Any) -> None:
            seen.calls.append(("review", (review,)))

    class Catalog:
        def __init__(self, store: Any, content: Any) -> None:
            seen.calls.append(("catalog", None))

    class Embedder:
        def __init__(self, config: Any) -> None:
            seen.calls.append(("embedder", config))

    class Projection:
        def __init__(self, config: Any, *, dimensions: int) -> None:
            seen.calls.append(("projection", (config, dimensions)))

    class Search:
        def __init__(self, store: Any, projection: Any, content: Any, embedder: Any, config: Any) -> None:
            seen.calls.append(("search", config))

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


def test_factory_without_an_embedding_endpoint_builds_no_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _patch_factory(monkeypatch, embedding=False)
    config = _config(**GIT_SKILLS)

    runtime = build_skill_runtime(config, session_factory=object())

    assert runtime.store is not None
    assert runtime.author is not None and runtime.review is not None
    assert runtime.catalog is not None
    assert runtime.search is None
    assert runtime.outbox is None
    assert runtime.projection is None
    assert [name for name, _ in seen.calls] == [
        "store",
        "git",
        "content",
        "author",
        "review",
        "catalog",
    ]


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
            }
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
