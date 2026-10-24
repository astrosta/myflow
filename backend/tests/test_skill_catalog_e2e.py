"""Task 16: the cutover path end to end.

一个用户私有起草并检索、管理员批准发布、另一个用户检索到公共版本的完整链路，
再加上两个运维脚本的契约：bootstrap 没有 --confirm 时拒绝运行，状态迁移可以重跑。

测试用本地 bare Git 仓库和假的 projection/embedder：不依赖 Milvus 或 PostgreSQL。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.config.skills_config import (
    SkillCreationConfig,
    SkillEmbeddingChannelConfig,
    SkillGitConfig,
    SkillReviewConfig,
    SkillSearchConfig,
)
from agentkit.persistence.schema import Base, SkillSettingsRow
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.catalog import SkillCatalogService, get_skill_snapshot
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import PackageManifest, StagedFile
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.outbox import SkillIndexOutboxWorker
from agentkit.skills.package import build_package
from agentkit.skills.projection import ProjectionHit, ProjectionRow
from agentkit.skills.review import SkillReviewService
from agentkit.skills.search import SkillSearchService
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
from fakes.skill_tree import write_app_config

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}


def valid_skill_bytes(name: str, description: str = "PDF") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        "## Steps\n1. Read the PDF.\n"
    ).encode()


def _git(*args: str, cwd: Path | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.stderr.decode('utf-8', 'replace')}"
        )
    return completed.stdout


def _make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(remote))
    seed = tmp_path / "seed"
    _git("init", "--initial-branch=main", str(seed))
    _git("config", "user.name", "Test Fixture", cwd=seed)
    _git("config", "user.email", "fixture@example.invalid", cwd=seed)
    (seed / "README.md").write_bytes(b"# tests\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    return remote


class FakeProjection:
    """在内存里模拟 active collection：唯一约束之外的可见性规则照抄 Milvus 过滤。"""

    def __init__(self) -> None:
        self.rows: dict[str, ProjectionRow] = {}

    async def upsert_active(self, row: ProjectionRow) -> None:
        self.rows[row.projection_key] = row

    async def delete_active(self, projection_key: str) -> None:
        self.rows.pop(projection_key, None)

    async def search(
        self,
        *,
        user_id: str,
        private_skill_ids: list[str] | tuple[str, ...],
        vector: list[float] | tuple[float, ...],
        limit: int,
    ) -> list[ProjectionHit]:
        blocked = set(private_skill_ids)
        hits: list[ProjectionHit] = []
        for row in self.rows.values():
            if row.scope == "public" and row.skill_id in blocked:
                continue
            if row.scope == "private" and row.owner_user_id != user_id:
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


@dataclass
class Harness:
    store: SqlSkillStore
    author: SkillAuthorService
    review: SkillReviewService
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

    remote = _make_remote(tmp_path)
    cache_dir = tmp_path / "cache"
    git = SkillGitRepository(
        SkillGitConfig(
            repository=remote.as_posix(),
            public_branch="main",
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
        author=SkillAuthorService(store, git, content, SkillCreationConfig()),
        review=SkillReviewService(store, git, content, SkillReviewConfig()),
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
        prefs=None,
    )


@pytest.mark.asyncio
async def test_author_private_search_then_approve_then_other_user_search(tmp_path: Path) -> None:
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
        await harness.author.save(owner_user_id="u1", skill_id=skill.id)
        await harness.outbox.run_once()

        own = await _search(harness, "u1")
        assert [hit.scope for hit in own.hits] == ["private"]
        assert own.hits[0].release_id is None

        sub = await harness.author.submit(owner_user_id="u1", skill_id=skill.id)
        release = await harness.review.approve(
            admin_user_id="admin", submission_id=sub.id
        )
        await harness.outbox.run_once()

        other = await _search(harness, "u2")
        assert [hit.scope for hit in other.hits] == ["public"]
        assert other.hits[0].release_id == release.id
        assert other.hits[0].skill.name == "pdf-tools"
        assert "Read the PDF" in other.hits[0].body
    finally:
        await harness.engine.dispose()




@pytest.mark.asyncio
async def test_save_and_delete_refresh_the_http_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await build_harness(tmp_path)
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
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
        review=harness.review,
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

        assert client.post(f"/api/skills/{skill_id}/draft/save").status_code == 200
        rows = client.get("/api/skills").json()["skills"]
        assert [(row["skill_id"], row["scope"]) for row in rows] == [(skill_id, "private")]
        assert rows[0]["container_file"] == "/mnt/skills/private/pdf-tools/SKILL.md"

        assert client.delete(f"/api/skills/{skill_id}").status_code == 204
        assert client.get("/api/skills").json()["skills"] == []
    finally:
        reset_skill_runtime()
        await harness.engine.dispose()


def test_bootstrap_refuses_without_confirm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from backend.scripts import bootstrap_public_skills

    source = tmp_path / "skills"
    (source / "public" / "pdf-tools").mkdir(parents=True)
    (source / "public" / "pdf-tools" / "SKILL.md").write_bytes(
        valid_skill_bytes("pdf-tools")
    )

    code = bootstrap_public_skills.main(
        ["--source", str(source), "--admin-user-id", "admin"]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "--confirm" in (captured.out + captured.err)
    # 什么都没做：没有草稿，也没有 Git 目录。
    assert not (tmp_path / "cache").exists()


@pytest.mark.asyncio
async def test_bootstrap_bumps_the_epoch_only_when_it_applies(tmp_path: Path) -> None:
    from backend.scripts import bootstrap_public_skills

    harness = await build_harness(tmp_path)
    source = tmp_path / "import"
    (source / "public" / "pdf-tools").mkdir(parents=True)
    (source / "public" / "pdf-tools" / "SKILL.md").write_bytes(
        valid_skill_bytes("pdf-tools")
    )
    try:
        assert await bootstrap_public_skills.bootstrap(
            store=harness.store,
            author=harness.author,
            review=harness.review,
            source=source,
            admin_user_id="admin",
            apply=False,
        ) == 1
        assert await harness.store.catalog_epoch() == 0

        assert await bootstrap_public_skills.bootstrap(
            store=harness.store,
            author=harness.author,
            review=harness.review,
            source=source,
            admin_user_id="admin",
            apply=True,
        ) == 1
        assert await harness.store.catalog_epoch() == 1
    finally:
        await harness.engine.dispose()


async def _publish_for_migration(harness: Harness) -> tuple[Any, Any]:
    """直接在 catalog 上落一条 public release：迁移只关心名字与开关。"""

    skill = await harness.store.create_skill(owner_user_id="admin", private_name="pdf-tools")
    files = {"SKILL.md": StagedFile("SKILL.md", valid_skill_bytes("pdf-tools"), "text")}
    manifest: PackageManifest = build_package(files, **LIMITS)
    version = await harness.store.record_saved_version(
        skill_id=skill.id,
        owner_user_id="admin",
        manifest=manifest,
        draft_ref="refs/heads/myflow/drafts/admin",
        draft_commit="a" * 40,
        skill_path=f"users/admin/{skill.id}/SKILL.md",
    )
    submission = await harness.store.create_submission(
        skill_id=skill.id,
        version=version,
        owner_user_id="admin",
        submitted_commit="a" * 40,
    )
    release = await harness.store.record_public_release(
        submission=submission,
        public_name="pdf-tools",
        git_repository="https://example.invalid/skills.git",
        git_commit="b" * 40,
        skill_path="public/pdf-tools/SKILL.md",
        source_commit="a" * 40,
        actor="admin",
    )
    return skill, release


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path: Path) -> None:
    from backend.scripts.migrate_skill_state import migrate

    harness = await build_harness(tmp_path)
    try:
        skill, _release = await _publish_for_migration(harness)

        extensions = tmp_path / "extensions_config.json"
        extensions.write_text(
            json.dumps(
                {
                    "skills": {"pdf-tools": {"enabled": False}},
                    "mcpServers": {"keep": {"command": "x"}},
                }
            ),
            encoding="utf-8",
        )
        prefs = tmp_path / "skills_prefs.json"
        prefs.write_text(
            json.dumps({"u1": {"pdf-tools": False, "unrelated": True}}),
            encoding="utf-8",
        )

        first = await migrate(
            store=harness.store,
            extensions_path=extensions,
            prefs_path=prefs,
            actor="migration",
            apply=True,
        )
        assert first.settings_written == 1
        assert first.pref_keys_rewritten == 1
        assert await harness.store.catalog_epoch() == 1

        rows = (await _all_settings(harness)).get(skill.id)
        assert rows is False
        preferences = json.loads(prefs.read_text(encoding="utf-8"))
        assert preferences == {"u1": {skill.id: False, "unrelated": True}}
        # 非 skills 的运维键原样保留。
        assert "mcpServers" in json.loads(extensions.read_text(encoding="utf-8"))

        snapshot = (extensions.read_bytes(), prefs.read_bytes())
        second = await migrate(
            store=harness.store,
            extensions_path=extensions,
            prefs_path=prefs,
            actor="migration",
            apply=True,
        )
        assert second.settings_written == 0
        assert second.pref_keys_rewritten == 0
        assert await harness.store.catalog_epoch() == 1
        assert (extensions.read_bytes(), prefs.read_bytes()) == snapshot

        extensions.write_text(
            json.dumps({"skills": {"pdf-tools": {"enabled": True}}}),
            encoding="utf-8",
        )
        dry = await migrate(
            store=harness.store,
            extensions_path=extensions,
            prefs_path=prefs,
            actor="migration",
            apply=False,
        )
        assert dry.settings_written == 1
        assert await harness.store.catalog_epoch() == 1
    finally:
        await harness.engine.dispose()


async def _all_settings(harness: Harness) -> dict[str, bool]:
    async with harness.store._sf() as session:  # noqa: SLF001 - read-only assertion
        rows = (await session.execute(select(SkillSettingsRow))).scalars().all()
    return {row.skill_id: row.enabled for row in rows}


# ---------------------------------------------------------------------------
# Cross-worker catalog invalidation (final review I1)
# ---------------------------------------------------------------------------


def _worker_runtime(harness: Harness, *, poll_seconds: float = 5.0) -> SkillRuntime:
    """A worker has its own store/session and catalog service over the shared DB."""
    session_factory = async_sessionmaker(harness.engine, expire_on_commit=False)
    store = SqlSkillStore(session_factory)
    return SkillRuntime(
        store=store,
        git=harness.git,
        content=harness.content,
        author=harness.author,
        review=harness.review,
        search=harness.search,
        catalog=SkillCatalogService(store, harness.content),
        catalog_poll_seconds=poll_seconds,
    )


async def _save_one_private_skill(harness: Harness) -> str:
    skill = await harness.author.create_draft(
        owner_user_id="u1", name="pdf-tools", description="PDF"
    )
    await harness.author.put_file(
        owner_user_id="u1",
        skill_id=skill.id,
        path="SKILL.md",
        content=valid_skill_bytes("pdf-tools"),
    )
    await harness.author.save(owner_user_id="u1", skill_id=skill.id)
    return skill.id


def _private_names(snapshot: Any, owner: str = "u1") -> list[str]:
    return [skill.name for skill in snapshot.private_by_owner.get(owner, ())]


@pytest.mark.asyncio
async def test_two_workers_see_a_save_after_exactly_one_epoch_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await build_harness(tmp_path)
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    config_path = write_app_config(
        tmp_path,
        source="git",
        git={"repository": (tmp_path / "remote.git").as_posix()},
    )
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(config_path))

    worker_a = _worker_runtime(harness)
    worker_b = _worker_runtime(harness)
    configure_skill_runtime(worker_a)
    try:
        await worker_a.refresh()
        b_before = await worker_b.refresh()
        assert _private_names(b_before) == []
        assert await worker_b.poll_catalog_epoch_once() is False

        await _save_one_private_skill(harness)
        await skills_catalog.refresh_skill_runtime()

        a_after = get_skill_snapshot()
        assert _private_names(a_after) == ["pdf-tools"]
        assert _private_names(b_before) == []
        assert worker_b._catalog_epoch != await worker_b.store.catalog_epoch()

        assert await worker_b.poll_catalog_epoch_once() is True
        b_after = get_skill_snapshot()
        assert _private_names(b_after) == ["pdf-tools"]

        assert await worker_b.poll_catalog_epoch_once() is False
        assert get_skill_snapshot() is b_after
    finally:
        reset_skill_runtime()
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_refresh_never_bumps_the_catalog_epoch(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    worker = _worker_runtime(harness)
    try:
        assert await worker.store.catalog_epoch() == 0
        await worker.refresh()
        await worker.refresh()
        assert await worker.store.catalog_epoch() == 0

        await worker.store.bump_catalog_epoch()
        for _ in range(3):
            await worker.refresh()
        assert await worker.store.catalog_epoch() == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_catalog_epoch_poller_starts_and_stops_with_the_runtime(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    worker = _worker_runtime(harness, poll_seconds=0.01)
    assert worker.outbox is None and worker.projection is None
    try:
        await worker.start()
        task = worker._catalog_epoch_task
        assert task is not None
        assert task.get_name() == "skill-catalog-epoch"
        assert not task.done()

        await worker.stop()
        assert task.cancelled()
        assert worker._catalog_epoch_task is None
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_a_database_without_the_epoch_table_still_starts_and_reports_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = await build_harness(tmp_path)
    async with harness.engine.begin() as conn:
        await conn.execute(text("DROP TABLE skill_catalog_epoch"))
    worker = _worker_runtime(harness, poll_seconds=5)
    caplog.set_level("ERROR", logger="backend.app.gateway.skills_catalog")
    try:
        # 缺表只丢跨 worker 失效提示；启动和本地服务必须继续。
        await worker.start()
        assert get_skill_snapshot() is not None
        await worker.refresh()
        await skills_catalog._bump_catalog_epoch(worker)
        assert caplog.text.count("skill_catalog_epoch is missing") == 1
        assert "alembic upgrade head" in caplog.text
    finally:
        await worker.stop()
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_catalog_poll_seconds_zero_starts_no_poller(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    worker = _worker_runtime(harness, poll_seconds=0)
    try:
        await worker.start()
        assert worker._catalog_epoch_task is None
        await worker.stop()
    finally:
        await harness.engine.dispose()
