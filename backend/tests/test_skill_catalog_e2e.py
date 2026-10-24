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
from sqlalchemy import select
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
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import PackageManifest, StagedFile
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.outbox import SkillIndexOutboxWorker
from agentkit.skills.package import build_package
from agentkit.skills.projection import ProjectionHit, ProjectionRow
from agentkit.skills.review import SkillReviewService
from agentkit.skills.search import SkillSearchService
from agentkit.skills.store.sql import SqlSkillStore

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
        assert (extensions.read_bytes(), prefs.read_bytes()) == snapshot
    finally:
        await harness.engine.dispose()


async def _all_settings(harness: Harness) -> dict[str, bool]:
    async with harness.store._sf() as session:  # noqa: SLF001 - read-only assertion
        rows = (await session.execute(select(SkillSettingsRow))).scalars().all()
    return {row.skill_id: row.enabled for row in rows}
