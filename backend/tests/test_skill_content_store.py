"""Task 5: immutable commit caches and per-scope active trees."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes.git_repo import make_bare_remote

from agentkit.config.skills_config import SkillGitConfig
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import (
    SkillScope,
    SkillVersionRecord,
    StagedFile,
    VisibleSkill,
)
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.types import Skill, SkillCategory

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _make_remote(tmp_path: Path) -> Path:
    return make_bare_remote(tmp_path)


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
    )


@dataclass
class ContentFixture:
    repo: SkillGitRepository
    content: SkillContentStore
    cache_dir: Path
    draft_one: object
    draft_two: object


@pytest.fixture(name="content_fixture")
def content_fixture(tmp_path: Path):
    remote = _make_remote(tmp_path)
    cache_dir = tmp_path / "cache"
    config = SkillGitConfig(
        repository=remote.as_posix(),
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
        return ContentFixture(
            repo=repo,
            content=content,
            cache_dir=cache_dir,
            draft_one=draft_one,
            draft_two=draft_two,
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
async def test_forget_commits_deletes_only_the_named_caches(
    content_fixture: ContentFixture,
):
    """事件驱动清理：只删调用方点名的提交，绝不扫别人目录。"""
    keep = content_fixture.draft_one.commit
    stale = "4" * 40
    untouched = "5" * 40
    commits = content_fixture.cache_dir / "commits"
    await content_fixture.content.ensure(keep)
    for name in (stale, untouched):
        (commits / name).mkdir(parents=True)
        (commits / name / "SKILL.md").write_bytes(b"---\n")

    assert await content_fixture.content.forget_commits({stale}) == 1

    assert not (commits / stale).exists()
    assert (commits / keep).is_dir()
    assert (commits / untouched).is_dir()  # 不在候选里就不动


@pytest.mark.asyncio
async def test_forget_commits_honours_the_ttl_window(
    content_fixture: ContentFixture,
):
    fresh = "6" * 40
    old = "7" * 40
    commits = content_fixture.cache_dir / "commits"
    for name in (fresh, old):
        (commits / name).mkdir(parents=True)
        (commits / name / "SKILL.md").write_bytes(b"---\n")
    two_days_ago = time.time() - 2 * 86400
    os.utime(commits / old / "SKILL.md", (two_days_ago, two_days_ago))
    os.utime(commits / old, (two_days_ago, two_days_ago))

    assert (
        await content_fixture.content.forget_commits({fresh, old}, min_age_seconds=86400)
        == 1
    )
    assert not (commits / old).exists()
    assert (commits / fresh).is_dir()


@pytest.mark.asyncio
async def test_a_forgotten_commit_is_re_materialized_from_git(
    content_fixture: ContentFixture,
):
    """缓存只是缓存：删掉之后下一次读会从 Git 重新物化。"""
    commit = content_fixture.draft_one.commit
    await content_fixture.content.ensure(commit)
    assert await content_fixture.content.forget_commits({commit}) == 1
    assert not (content_fixture.cache_dir / "commits" / commit).exists()

    text = await content_fixture.content.read_text(
        commit, "users/u1/s1/SKILL.md"
    )

    assert "name:" in text
    assert (content_fixture.cache_dir / "commits" / commit).is_dir()

@pytest.mark.asyncio
async def test_a_second_text_read_is_served_from_the_lru(
    content_fixture: ContentFixture,
):
    """BM25 通道每次检索都读可见集合的正文：第二次读不该再碰磁盘。"""

    commit = content_fixture.draft_one.commit
    reads: list[tuple[str, str]] = []
    original = content_fixture.content.read_bytes

    async def counting_read_bytes(commit_id: str, path: str) -> bytes:
        reads.append((commit_id, path))
        return await original(commit_id, path)

    content_fixture.content.read_bytes = counting_read_bytes  # type: ignore[method-assign]

    path = 'users/u1/s1/SKILL.md'
    first = await content_fixture.content.read_text(commit, path)
    second = await content_fixture.content.read_text(commit, path)

    assert first == second
    assert len(reads) == 1


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
        catalog=None,
    )

    await runtime.materialize_active_trees()

    # 空树换掉旧树：目录还在（挂载点存在），但里面一个技能都不剩。
    assert not list(tree.iterdir()) if tree.exists() else True
