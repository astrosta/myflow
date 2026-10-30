"""Small pygit2 fixtures shared by skill Git integration tests."""

from __future__ import annotations

from pathlib import Path

import pygit2

_SIGNATURE = pygit2.Signature("Test Fixture", "fixture@example.invalid")


def make_bare_remote(tmp_path: Path) -> Path:
    """Create a bare repository with main and one seed commit, without git(1)."""

    remote = tmp_path / "remote.git"
    repo = pygit2.init_repository(str(remote), bare=True, initial_head="main")
    builder = repo.TreeBuilder()
    builder.insert("README.md", repo.create_blob(b"# tests\n"), 0o100644)
    repo.create_commit(
        "refs/heads/main",
        _SIGNATURE,
        _SIGNATURE,
        "seed",
        builder.write(),
        [],
    )
    return remote


def remote_ref(remote: Path, ref: str) -> str:
    return str(pygit2.Repository(str(remote)).references[ref].target)


def tree_entry(remote: Path, commit: str, path: str) -> pygit2.TreeEntry:
    repo = pygit2.Repository(str(remote))
    tree = repo.revparse_single(commit).tree
    parts = path.split("/")
    for part in parts[:-1]:
        tree = repo[tree[part].id]
    return tree[parts[-1]]


def file_mode(remote: Path, commit: str, path: str) -> bytes:
    return f"{int(tree_entry(remote, commit, path).filemode):o}".encode()


def make_symlink_commit(remote: Path) -> str:
    repo = pygit2.Repository(str(remote))
    builder = repo.TreeBuilder()
    builder.insert(
        "SKILL.md",
        repo.create_blob(b"---\nname: evil\ndescription: x\n---\n"),
        0o100644,
    )
    builder.insert("link", repo.create_blob(b"../../outside"), 0o120000)
    commit = repo.create_commit(
        "refs/heads/malicious",
        _SIGNATURE,
        _SIGNATURE,
        "malicious",
        builder.write(),
        [],
    )
    return str(commit)
