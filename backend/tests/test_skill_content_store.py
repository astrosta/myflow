"""Task 5: immutable commit caches and per-scope active trees."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentkit.config.skills_config import SkillGitConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import (
    SkillReleaseRecord,
    SkillScope,
    SkillVersionRecord,
    StagedFile,
    VisibleSkill,
)
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.types import Skill, SkillCategory

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _git(*args: str, cwd: Path | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
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


def _skill(
    *,
    skill_id: str,
    name: str,
    owner: str | None,
    scope: SkillScope,
    version_id: str,
) -> Skill:
    return Skill(
        name=name,
        description="description",
        license=None,
        skill_dir=Path("unused"),
        skill_file=Path("unused/SKILL.md"),
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        skill_id=skill_id,
        scope=scope,
        owner_user_id=owner,
        version_id=version_id,
    )


def _private(
    *,
    skill_id: str,
    name: str,
    owner: str,
    commit: str,
    path: str,
) -> VisibleSkill:
    return VisibleSkill(
        skill=_skill(
            skill_id=skill_id,
            name=name,
            owner=owner,
            scope=SkillScope.PRIVATE,
            version_id=f"version-{skill_id}",
        ),
        scope=SkillScope.PRIVATE,
        version=SkillVersionRecord(
            id=f"version-{skill_id}",
            skill_id=skill_id,
            skill_version="v1",
            version_seq=1,
            package_sha256="0" * 64,
            skill_md_sha256="0" * 64,
            metadata={},
            draft_ref=f"refs/heads/myflow/drafts/{owner}",
            draft_commit=commit,
            skill_path=path,
            index_status="ready",
            created_at=NOW,
        ),
        release=None,
    )


def _public(
    *,
    skill_id: str,
    public_name: str,
    owner: str,
    version_commit: str,
    version_path: str,
    release_commit: str,
    release_path: str,
) -> VisibleSkill:
    version = SkillVersionRecord(
        id=f"version-{skill_id}",
        skill_id=skill_id,
        skill_version="v1",
        version_seq=1,
        package_sha256="0" * 64,
        skill_md_sha256="0" * 64,
        metadata={},
        draft_ref=f"refs/heads/myflow/drafts/{owner}",
        draft_commit=version_commit,
        skill_path=version_path,
        index_status="ready",
        created_at=NOW,
    )
    release = SkillReleaseRecord(
        id=f"release-{skill_id}",
        batch_id="batch",
        version_id=version.id,
        skill_id=skill_id,
        skill_version="v1",
        git_repository="origin",
        git_commit=release_commit,
        skill_path=release_path,
        public_name=public_name,
        source_commit=version_commit,
        approved_by="admin",
        created_at=NOW,
    )
    return VisibleSkill(
        skill=_skill(
            skill_id=skill_id,
            name=public_name,
            owner=owner,
            scope=SkillScope.PUBLIC,
            version_id=version.id,
        ),
        scope=SkillScope.PUBLIC,
        version=version,
        release=release,
    )


@dataclass
class ContentFixture:
    repo: SkillGitRepository
    content: SkillContentStore
    cache_dir: Path
    draft_one: object
    draft_two: object
    public: object


@pytest.fixture(name="content_fixture")
def content_fixture(tmp_path: Path):
    remote = _make_remote(tmp_path)
    cache_dir = tmp_path / "cache"
    config = SkillGitConfig(
        repository=remote.as_posix(),
        public_branch="main",
        cache_dir=str(cache_dir),
        command_timeout_seconds=30,
    )
    repo = SkillGitRepository(config, cache_dir)
    content = SkillContentStore(cache_dir, repo)

    async def _build() -> ContentFixture:
        await repo.ensure_ready()
        draft_one = await repo.save_draft(
            owner_user_id="u1",
            skill_id="s1",
            files=[
                StagedFile("SKILL.md", b"---\nname: foo\ndescription: one\n---\n"),
                StagedFile("scripts/run.sh", b"echo one\n", "executable"),
            ],
            message="one",
        )
        draft_two = await repo.save_draft(
            owner_user_id="u1",
            skill_id="s2",
            files=[StagedFile("SKILL.md", b"---\nname: bar\ndescription: two\n---\n")],
            message="two",
        )
        public = await repo.publish_public(
            public_name="foo",
            source_commit=draft_one.commit,
            source_path="users/u1/s1",
            message="approve",
        )
        return ContentFixture(
            repo=repo,
            content=content,
            cache_dir=cache_dir,
            draft_one=draft_one,
            draft_two=draft_two,
            public=public,
        )

    return asyncio.run(_build())


@pytest.mark.asyncio
async def test_ensure_materializes_and_reads_the_commit_cache(content_fixture: ContentFixture):
    commit_dir = await content_fixture.content.ensure(content_fixture.draft_one.commit)
    assert commit_dir == content_fixture.cache_dir / "commits" / content_fixture.draft_one.commit
    assert commit_dir.is_dir()
    assert (commit_dir / "users/u1/s1/SKILL.md").read_bytes().startswith(b"---")
    assert await content_fixture.content.read_text(
        content_fixture.draft_one.commit, "users/u1/s1/scripts/run.sh"
    ) == "echo one\n"
    assert await content_fixture.content.read_bytes(
        content_fixture.draft_one.commit, "users/u1/s1/SKILL.md"
    ) == b"---\nname: foo\ndescription: one\n---\n"


@pytest.mark.asyncio
async def test_read_and_ensure_reject_unsafe_commit_or_path(content_fixture: ContentFixture):
    with pytest.raises(ValueError):
        await content_fixture.content.ensure("../outside")
    with pytest.raises(ValueError):
        await content_fixture.content.read_text(
            content_fixture.draft_one.commit, "../outside"
        )


@pytest.mark.asyncio
async def test_materialize_public_uses_the_release_commit_and_replaces_stale_entries(
    content_fixture: ContentFixture,
):
    visible = _public(
        skill_id="s1",
        public_name="foo",
        owner="u1",
        version_commit=content_fixture.draft_one.commit,
        version_path="users/u1/s1/SKILL.md",
        release_commit=content_fixture.public.commit,
        release_path="public/foo/SKILL.md",
    )
    tree = await content_fixture.content.materialize_public([visible])
    assert tree == content_fixture.cache_dir / "active" / "public"
    assert (tree / "foo/SKILL.md").read_bytes().startswith(b"---")
    assert (tree / "foo/scripts/run.sh").read_bytes() == b"echo one\n"

    (tree / "old").mkdir()
    (tree / "old/SKILL.md").write_bytes(b"stale")
    tree_again = await content_fixture.content.materialize_public([visible])
    assert tree_again == tree
    assert not (tree / "old").exists()


@pytest.mark.asyncio
async def test_materialize_private_is_owner_scoped_and_replaces_the_tree(
    content_fixture: ContentFixture,
):
    one = _private(
        skill_id="s1",
        name="foo",
        owner="u1",
        commit=content_fixture.draft_one.commit,
        path="users/u1/s1/SKILL.md",
    )
    two = _private(
        skill_id="s2",
        name="bar",
        owner="u1",
        commit=content_fixture.draft_two.commit,
        path="users/u1/s2/SKILL.md",
    )
    other = _private(
        skill_id="s9",
        name="other",
        owner="u2",
        commit=content_fixture.draft_two.commit,
        path="users/u1/s2/SKILL.md",
    )

    tree = await content_fixture.content.materialize_private("u1", [one, two, other])
    assert tree == content_fixture.cache_dir / "active" / "users" / "u1"
    assert sorted(path.name for path in tree.iterdir()) == ["bar", "foo"]
    assert (tree / "foo/SKILL.md").read_bytes().startswith(b"---")
    assert not (tree / "other").exists()

    narrowed = await content_fixture.content.materialize_private("u1", [two])
    assert narrowed == tree
    assert not (tree / "foo").exists()
    assert (tree / "bar/SKILL.md").read_bytes().startswith(b"---")


@pytest.mark.asyncio
async def test_prune_removes_only_cache_directories_not_in_the_keep_set(
    content_fixture: ContentFixture,
):
    keep = content_fixture.draft_one.commit
    stale = "1" * 40
    await content_fixture.content.ensure(keep)
    commits = content_fixture.cache_dir / "commits"
    (commits / stale).mkdir()
    (commits / stale / "junk").write_bytes(b"junk")
    unrelated = commits / "notes.txt"
    unrelated.write_bytes(b"keep me")

    assert await content_fixture.content.prune({keep}) == 1
    assert (commits / keep).is_dir()
    assert not (commits / stale).exists()
    assert unrelated.read_bytes() == b"keep me"
    assert await content_fixture.content.prune({keep, stale}) == 0


@pytest.mark.asyncio
async def test_prune_keeps_unreferenced_caches_inside_the_ttl_window(
    content_fixture: ContentFixture,
):
    """retention.cache_ttl_days 是安全期：刚失去引用的缓存先别删。"""

    keep = content_fixture.draft_one.commit
    fresh = "2" * 40
    old = "3" * 40
    commits = content_fixture.cache_dir / "commits"
    for name in (fresh, old):
        (commits / name).mkdir(parents=True)
        (commits / name / "SKILL.md").write_bytes(b"---\n")

    # old 目录的最后修改时间被压到两天前：TTL 一天之外，可以删。
    two_days_ago = time.time() - 2 * 86400
    os.utime(commits / old / "SKILL.md", (two_days_ago, two_days_ago))
    os.utime(commits / old, (two_days_ago, two_days_ago))

    assert await content_fixture.content.prune({keep}, min_age_seconds=86400) == 1
    assert not (commits / old).exists()
    assert (commits / fresh).is_dir()

    # 安全期清零时，剩下那个也该被删。
    assert await content_fixture.content.prune({keep}) == 1
    assert not (commits / fresh).exists()

@pytest.mark.asyncio
async def test_existing_private_owners_lists_the_materialized_trees(
    content_fixture: ContentFixture,
):
    assert content_fixture.content.existing_private_owners() == []

    one = _private(
        skill_id="s1",
        name="foo",
        owner="u1",
        commit=content_fixture.draft_one.commit,
        path="users/u1/s1/SKILL.md",
    )
    await content_fixture.content.materialize_private("u1", [one])
    await content_fixture.content.materialize_private("u2", [])

    assert content_fixture.content.existing_private_owners() == ["u1", "u2"]


@pytest.mark.asyncio
async def test_refresh_prunes_the_real_tree_of_an_owner_who_lost_every_skill(
    content_fixture: ContentFixture,
):
    from backend.app.gateway import skills_catalog

    one = _private(
        skill_id="s1",
        name="foo",
        owner="u1",
        commit=content_fixture.draft_one.commit,
        path="users/u1/s1/SKILL.md",
    )
    await content_fixture.content.materialize_private("u1", [one])
    tree = content_fixture.cache_dir / "active" / "users" / "u1"
    assert (tree / "foo" / "SKILL.md").is_file()

    class _Rows:
        def __init__(self, rows: list) -> None:
            self.rows = rows

        async def all_active(self) -> list:
            return list(self.rows)

    runtime = skills_catalog.SkillRuntime(
        store=_Rows([]),
        git=None,
        content=content_fixture.content,
        author=None,
        review=None,
        catalog=None,
    )

    await runtime.materialize_active_trees()

    # 空树换掉旧树：目录还在（挂载点存在），但里面一个技能都不剩。
    assert not list(tree.iterdir()) if tree.exists() else True
