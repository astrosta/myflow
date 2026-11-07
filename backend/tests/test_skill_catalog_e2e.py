"""End-to-end: private save -> scan -> activate -> retrieval and HTTP catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.config.skills_config import (
    SkillCreationConfig,
    SkillEmbeddingChannelConfig,
    SkillGitConfig,
    SkillSearchConfig,
)
from agentkit.persistence.schema import Base
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.catalog import SkillCatalogService
from agentkit.skills.content import SkillContentStore
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.outbox import SkillIndexOutboxWorker
from agentkit.skills.projection import ProjectionHit, ProjectionRow
from agentkit.skills.search import SkillSearchService
from agentkit.skills.security_scan import SecurityScanOutcome
from agentkit.skills.store.sql import SqlSkillStore
from backend.app.gateway import deps, skills_catalog
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import skill_drafts, skills
from backend.app.gateway.run_models import CallerKind, RunCaller
from backend.app.gateway.skills_catalog import (
    SkillRuntime,
    configure_skill_runtime,
    reset_skill_runtime,
)
from fakes.git_repo import make_bare_remote
from fakes.skill_tree import write_app_config


def valid_skill_bytes(name: str, description: str = "PDF") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        "## Steps\n1. Read the PDF.\n"
    ).encode()


class FakeProjection:
    def __init__(self) -> None:
        self.rows: dict[str, ProjectionRow] = {}

    async def upsert_active(self, row: ProjectionRow) -> None:
        self.rows[row.projection_key] = row

    async def delete_active(self, projection_key: str) -> None:
        self.rows.pop(projection_key, None)

    async def scan_projection_keys(self, *, limit: int) -> tuple[list[str], bool]:
        keys = list(self.rows)[: max(0, int(limit))]
        return keys, len(self.rows) > len(keys)

    async def search(
        self,
        *,
        user_id: str,
        vector: list[float] | tuple[float, ...],
        limit: int,
    ) -> list[ProjectionHit]:
        hits: list[ProjectionHit] = []
        for row in self.rows.values():
            if row.scope != "private" or row.owner_user_id != user_id:
                continue
            hits.append(
                ProjectionHit(
                    projection_key=row.projection_key,
                    scope=row.scope,
                    owner_user_id=row.owner_user_id,
                    skill_id=row.skill_id,
                    version_id=row.version_id,
                    score=0.9,
                )
            )
        return hits[:limit]


class FakeEmbedder:
    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    async def embed_documents(self, texts: list[str] | tuple[str, ...]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeScanner:
    def scan(self, skill_directory: Path) -> SecurityScanOutcome:
        assert (skill_directory / "SKILL.md").is_file()
        return SecurityScanOutcome(
            status="completed",
            is_safe=True,
            max_severity="SAFE",
            scanner_version="2.3.2",
            duration_ms=1,
        )


@dataclass
class Harness:
    store: SqlSkillStore
    author: SkillAuthorService
    outbox: SkillIndexOutboxWorker
    search: SkillSearchService
    content: SkillContentStore
    git: SkillGitRepository
    catalog: SkillCatalogService
    projection: FakeProjection = field(default_factory=FakeProjection)
    engine: Any = None


async def build_harness(tmp_path: Path) -> Harness:
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = SqlSkillStore(factory)

    remote = make_bare_remote(tmp_path)
    cache_dir = tmp_path / "cache"
    git = SkillGitRepository(
        SkillGitConfig(
            repository=remote.as_posix(),
            cache_dir=str(cache_dir),
            command_timeout_seconds=30,
        ),
        cache_dir,
    )
    await git.ensure_ready()
    content = SkillContentStore(cache_dir, git)
    projection = FakeProjection()
    embedder = FakeEmbedder()
    search_config = SkillSearchConfig(
        embedding=SkillEmbeddingChannelConfig(
            enabled=True, base_url="http://embed.invalid", dimensions=3
        )
    )
    return Harness(
        store=store,
        author=SkillAuthorService(
            store, git, content, SkillCreationConfig(), scanner=FakeScanner()
        ),
        outbox=SkillIndexOutboxWorker(store, content, embedder, projection, poll_seconds=1),
        search=SkillSearchService(store, projection, content, embedder, search_config),
        content=content,
        git=git,
        catalog=SkillCatalogService(store, content),
        projection=projection,
        engine=engine,
    )


async def _search(harness: Harness, user_id: str):
    return await harness.search.search(
        user_id=user_id,
        task_query="PDF",
        domain_keywords=[],
        available_skills=None,
    )


@pytest.mark.asyncio
async def test_save_scan_activate_then_only_the_owner_can_retrieve(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    try:
        skill = await harness.author.create_draft(
            owner_user_id="u1", name="pdf-tools", description="PDF"
        )
        await harness.author.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=valid_skill_bytes("pdf-tools"),
        )
        version = await harness.author.save(owner_user_id="u1", skill_id=skill.id)
        await harness.outbox.run_once()

        # Saving commits the draft to Git, but stays inert until activation.
        assert version.draft_commit is not None
        assert await harness.git.head("refs/heads/myflow/drafts/u1") == version.draft_commit

        # Saving alone is inert: no active pointer, no projection row, no hit.
        assert (await _search(harness, "u1")).hits == ()
        assert harness.projection.rows == {}

        scan = await harness.author.scan_security(owner_user_id="u1", skill_id=skill.id)
        assert scan.version_id == version.id and scan.is_safe is True
        active = await harness.author.activate(owner_user_id="u1", skill_id=skill.id)
        await harness.outbox.run_once()
        assert active.id == version.id
        assert active.draft_commit is not None
        assert await harness.git.head("refs/heads/myflow/drafts/u1") == active.draft_commit
        assert len(harness.projection.rows) == 1

        own = await _search(harness, "u1")
        assert [hit.scope for hit in own.hits] == ["private"]
        assert own.hits[0].skill.skill_id == skill.id
        assert "Read the PDF" in own.hits[0].body

        assert (await _search(harness, "u2")).hits == ()
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_http_save_scan_activate_and_delete_refresh_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await build_harness(tmp_path)
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    config_path = write_app_config(
        tmp_path,
        source="git",
        git={"repository": (tmp_path / "remote.git").as_posix()},
    )
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(config_path))

    runtime = SkillRuntime(
        store=harness.store,
        git=harness.git,
        content=harness.content,
        author=harness.author,
        search=harness.search,
        catalog=harness.catalog,
        outbox=harness.outbox,
    )
    configure_skill_runtime(runtime)
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.state.skill_runtime = runtime
    application.state.skill_author_service = harness.author
    application.include_router(skill_drafts.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    monkeypatch.setattr(
        deps,
        "caller_from_request",
        lambda request: RunCaller(
            kind=CallerKind.HTTP,
            user_id="u1",
            scopes=frozenset({"skills:read", "skills:write"}),
        ),
    )
    client = TestClient(application, raise_server_exceptions=False)
    try:
        created = client.post(
            "/api/skills", json={"name": "pdf-tools", "description": "PDF"}
        )
        assert created.status_code == 201, created.text
        skill_id = created.json()["id"]
        assert client.put(
            f"/api/skills/{skill_id}/draft/file",
            json={"path": "SKILL.md", "content": valid_skill_bytes("pdf-tools").decode()},
        ).status_code == 200

        saved = client.post(f"/api/skills/{skill_id}/draft/save")
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"]["draft_commit"] is not None
        # Saving commits the draft but the runtime catalog is still empty.
        assert client.get("/api/skills").json()["skills"] == []

        scanned = client.post(f"/api/skills/{skill_id}/scan")
        assert scanned.status_code == 200, scanned.text
        assert scanned.json()["is_safe"] is True

        activated = client.post(f"/api/skills/{skill_id}/activate", json={})
        assert activated.status_code == 200, activated.text
        assert activated.json()["version"]["draft_commit"] is not None
        rows = client.get("/api/skills").json()["skills"]
        assert [(row["skill_id"], row["scope"], row["status"]) for row in rows] == [
            (skill_id, "private", "active")
        ]

        assert client.delete(f"/api/skills/{skill_id}").status_code == 204
        assert client.get("/api/skills").json()["skills"] == []
    finally:
        reset_skill_runtime()
        await harness.engine.dispose()