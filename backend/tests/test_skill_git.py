"""Task 5: the only code allowed to write Git."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pygit2
import pytest
from fakes.git_repo import file_mode, make_bare_remote, make_symlink_commit, remote_ref

from agentkit.config.skills_config import SkillGitConfig
from agentkit.skills.domain import InvalidSkillPackage, StagedFile
from agentkit.skills.git import GitCommandError, GitCommit, SkillGitRepository
from agentkit.skills.package import build_package


def _make_remote(tmp_path: Path) -> Path:
    return make_bare_remote(tmp_path)


@dataclass
class GitFixture:
    repo: SkillGitRepository
    remote: Path
    cache_dir: Path
    draft: GitCommit

    async def delete_draft_ref(self) -> None:
        pygit2.Repository(str(self.remote)).references.delete(
            "refs/heads/myflow/drafts/u1"
        )

    def make_symlink_commit(self) -> str:
        return make_symlink_commit(self.remote)


@pytest.fixture(name="git_fixture")
def git_fixture(tmp_path: Path):
    remote = _make_remote(tmp_path)
    cache_dir = tmp_path / "cache"
    config = SkillGitConfig(
        repository=remote.as_posix(),
        public_branch="main",
        cache_dir=str(cache_dir),
        command_timeout_seconds=30,
    )
    repo = SkillGitRepository(config, cache_dir)

    async def _build() -> GitFixture:
        await repo.ensure_ready()
        draft = await repo.save_draft(
            owner_user_id="u1",
            skill_id="s1",
            files=[
                StagedFile(
                    "SKILL.md",
                    b"---\nname: foo\ndescription: x\n---\nbody\n",
                ),
                StagedFile("scripts/run.sh", b"echo ok\n", "executable"),
            ],
            message="draft",
        )
        return GitFixture(repo=repo, remote=remote, cache_dir=cache_dir, draft=draft)

    return asyncio.run(_build())


@pytest.mark.asyncio
async def test_save_draft_pushes_private_tree(git_fixture: GitFixture):
    commit = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s1",
        files=[
            StagedFile(
                "SKILL.md",
                b"---\nname: foo\ndescription: x\n---\nbody\n",
            ),
            StagedFile("scripts/run.sh", b"echo ok\n"),
        ],
        message="draft",
    )
    assert commit.ref == "refs/heads/myflow/drafts/u1"
    assert commit.path == "users/u1/s1"
    assert (
        await git_fixture.repo.read_file(commit.commit, "users/u1/s1/scripts/run.sh")
        == b"echo ok\n"
    )
    assert remote_ref(git_fixture.remote, "refs/heads/myflow/drafts/u1") == commit.commit
    assert await git_fixture.repo.head(commit.ref) == commit.commit


@pytest.mark.asyncio
async def test_save_and_publish_preserve_the_executable_bit(git_fixture: GitFixture):
    draft_mode = file_mode(
        git_fixture.remote,
        git_fixture.draft.commit,
        "users/u1/s1/scripts/run.sh",
    )
    assert draft_mode == b"100755"

    public = await git_fixture.repo.publish_public(
        public_name="foo",
        source_commit=git_fixture.draft.commit,
        source_path="users/u1/s1",
        message="approve",
    )
    public_mode = file_mode(
        git_fixture.remote,
        public.commit,
        "public/foo/scripts/run.sh",
    )
    assert public_mode == b"100755"


@pytest.mark.asyncio
async def test_publish_public_copies_the_exact_tree_and_survives_draft_deletion(
    git_fixture: GitFixture,
):
    public = await git_fixture.repo.publish_public(
        public_name="foo",
        source_commit=git_fixture.draft.commit,
        source_path="users/u1/s1",
        message="approve",
    )
    assert public.ref == "refs/heads/main"
    assert public.path == "public/foo"
    assert (
        await git_fixture.repo.read_file(public.commit, "public/foo/SKILL.md")
    ).startswith(b"---")
    await git_fixture.delete_draft_ref()
    assert await git_fixture.repo.read_file(public.commit, "public/foo/SKILL.md")


@pytest.mark.asyncio
async def test_second_save_replaces_only_that_skill_directory(git_fixture: GitFixture):
    await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s2",
        files=[StagedFile("SKILL.md", b"---\nname: bar\ndescription: y\n---\n")],
        message="second",
    )
    replacement = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s1",
        files=[
            StagedFile(
                "SKILL.md",
                b"---\nname: foo\ndescription: z\n---\nchanged\n",
            )
        ],
        message="replace",
    )
    assert await git_fixture.repo.list_tree(replacement.commit, "users/u1/s1") == [
        "SKILL.md"
    ]
    assert await git_fixture.repo.read_file(
        replacement.commit, "users/u1/s1/SKILL.md"
    ) == b"---\nname: foo\ndescription: z\n---\nchanged\n"
    assert await git_fixture.repo.read_file(
        replacement.commit, "users/u1/s2/SKILL.md"
    ) == b"---\nname: bar\ndescription: y\n---\n"


@pytest.mark.asyncio
async def test_publish_replaces_the_old_public_directory(git_fixture: GitFixture):
    first = await git_fixture.repo.publish_public(
        public_name="foo",
        source_commit=git_fixture.draft.commit,
        source_path="users/u1/s1",
        message="first",
    )
    assert await git_fixture.repo.list_tree(first.commit, "public/foo") == [
        "SKILL.md",
        "scripts/run.sh",
    ]

    changed = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s1",
        files=[StagedFile("SKILL.md", b"---\nname: foo\ndescription: v2\n---\n")],
        message="changed",
    )
    second = await git_fixture.repo.publish_public(
        public_name="foo",
        source_commit=changed.commit,
        source_path="users/u1/s1/SKILL.md",
        message="second",
    )
    assert await git_fixture.repo.list_tree(second.commit, "public/foo") == ["SKILL.md"]
    assert await git_fixture.repo.read_file(
        second.commit, "public/foo/SKILL.md"
    ) == b"---\nname: foo\ndescription: v2\n---\n"


@pytest.mark.asyncio
async def test_parent_traversal_is_rejected_before_git(git_fixture: GitFixture):
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.save_draft(
            owner_user_id="u1",
            skill_id="s1",
            files=[StagedFile("../escape", b"owned")],
            message="escape",
        )
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.read_file(git_fixture.draft.commit, "users/u1/s1/../../x")


@pytest.mark.asyncio
async def test_newline_in_a_ref_or_path_segment_is_rejected(git_fixture: GitFixture):
    files = [StagedFile("SKILL.md", b"---\nname: foo\ndescription: x\n---\n")]
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.save_draft(
            owner_user_id="u1\nstatus", skill_id="s1", files=files, message="bad"
        )
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.save_draft(
            owner_user_id="u1", skill_id="s1\nfoo", files=files, message="bad"
        )
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.publish_public(
            public_name="foo\nbar",
            source_commit=git_fixture.draft.commit,
            source_path="users/u1/s1",
            message="bad",
        )


@pytest.mark.asyncio
async def test_materialize_extracts_a_commit_without_links(
    git_fixture: GitFixture, tmp_path: Path
):
    destination = tmp_path / "materialized"
    await git_fixture.repo.materialize(git_fixture.draft.commit, destination)
    assert (destination / "users/u1/s1/SKILL.md").read_bytes().startswith(b"---")
    assert (destination / "users/u1/s1/scripts/run.sh").read_bytes() == b"echo ok\n"


@pytest.mark.asyncio
async def test_materialize_rejects_symlink_archive_entries(
    git_fixture: GitFixture, tmp_path: Path
):
    commit = git_fixture.make_symlink_commit()
    destination = tmp_path / "materialized"
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.materialize(commit, destination)
    assert not destination.exists()


@pytest.mark.asyncio
async def test_materialize_rejects_oversized_archives(
    git_fixture: GitFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agentkit.skills.git as git_module

    monkeypatch.setattr(git_module, "MAX_ARCHIVE_BYTES", 32)
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.materialize(git_fixture.draft.commit, tmp_path / "out")


@pytest.mark.asyncio
async def test_package_sha256_matches_the_canonical_package_hash(
    git_fixture: GitFixture,
):
    directory = "users/u1/s1"
    skill = StagedFile(
        "SKILL.md", b"---\nname: foo\ndescription: x\n---\nbody\n", "text"
    )
    script = StagedFile("scripts/run.sh", b"echo ok\n", "executable")
    expected = build_package(
        {item.path: item for item in (skill, script)},
        max_files=20,
        max_file_bytes=4096,
        max_package_bytes=16384,
    ).package_sha256

    first = await git_fixture.repo.package_sha256(
        git_fixture.draft.commit, directory
    )
    assert first == expected
    assert (
        await git_fixture.repo.package_sha256(
            git_fixture.draft.commit, f"{directory}/SKILL.md"
        )
        == first
    )

    changed_content = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s1",
        files=[skill, StagedFile("scripts/run.sh", b"echo changed\n", "executable")],
        message="changed content",
    )
    assert await git_fixture.repo.package_sha256(changed_content.commit, directory) != first

    changed_mode = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s1",
        files=[skill, StagedFile("scripts/run.sh", b"echo ok\n", "text")],
        message="changed mode",
    )
    assert await git_fixture.repo.package_sha256(changed_mode.commit, directory) != first


@pytest.mark.asyncio
async def test_package_sha256_rejects_an_empty_tree(git_fixture: GitFixture):
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.package_sha256(
            git_fixture.draft.commit, "users/u1/missing"
        )


@pytest.mark.asyncio
async def test_package_sha256_rejects_a_missing_skill_md(git_fixture: GitFixture):
    saved = await git_fixture.repo.save_draft(
        owner_user_id="u1",
        skill_id="s2",
        files=[StagedFile("notes.txt", b"no skill\n")],
        message="missing skill file",
    )
    with pytest.raises(InvalidSkillPackage):
        await git_fixture.repo.package_sha256(saved.commit, "users/u1/s2")


@pytest.mark.asyncio
async def test_git_command_error_carries_the_command_and_stderr(git_fixture: GitFixture):
    with pytest.raises(GitCommandError) as caught:
        await git_fixture.repo.read_file(git_fixture.draft.commit, "users/u1/s1/missing")
    assert caught.value.returncode != 0
    assert "missing" in caught.value.stderr
    assert caught.value.command[-1].endswith("missing")
