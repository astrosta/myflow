"""Restricted Git writer for the skill catalog.

This module is the only place in the application allowed to mutate Git.  It
uses pygit2/libgit2 directly, so the deployment does not need a git executable,
hooks, credential helpers, or an interactive prompt.  Content entering or
leaving Git is validated as an exact file set; trees are read and written
object-by-object rather than through a working tree or archive extractor.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

import pygit2

from agentkit.config.skills_config import SkillGitConfig
from agentkit.skills.domain import InvalidSkillPackage, StagedFile
from agentkit.skills.package import validate_skill_path

SERVICE_NAME = "MyFlow Skills Service"
SERVICE_EMAIL = "skills-service@myflow.local"

# The limits bound one materialized commit.  They are deliberately higher than
# one skill package because a public commit contains every active skill.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_FILE_BYTES = 16 * 1024 * 1024

_HEX = frozenset("0123456789abcdefABCDEF")
_REF_FORBIDDEN = frozenset(" ~^:?*[\\")
_SKILL_FILE = "SKILL.md"
_NUL = b"\x00"
_TREE_MODE = 0o040000
_BLOB_MODES = frozenset({0o100644, 0o100755})


@dataclass(frozen=True, slots=True)
class GitCommit:
    """One commit created by this service."""

    commit: str
    ref: str
    path: str


class GitCommandError(RuntimeError):
    """A libgit2 operation failed; the detail is retained for diagnostics.

    The historical name and the command/returncode/stderr shape are kept so
    callers and tests that report the failed operation remain compatible.
    """

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        shown = " ".join(self.command)
        detail = stderr.strip() or "no detail"
        super().__init__(f"git operation failed ({returncode}): {shown}: {detail}")


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    oid: str


class _RemoteCallbacks(pygit2.RemoteCallbacks):
    """Credentials plus a wall-clock timeout for one remote operation.

    libgit2 has no per-operation timeout.  Progress callbacks are the only
    interruption point it exposes, so a stalled transfer fails through this
    callback instead of hanging forever.  The push_update_reference callback
    also records server-side rejections, which pygit2 otherwise does not expose.
    """

    def __init__(self, *, timeout: float, credentials: Any | None = None) -> None:
        super().__init__(credentials=credentials)
        self._timeout = float(timeout)
        self._deadline = time.monotonic() + self._timeout if self._timeout > 0 else None
        self.rejections: list[tuple[str, str]] = []

    def _check_timeout(self) -> None:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise GitCommandError(
                ("pygit2", "network"),
                -1,
                f"timed out after {self._timeout:g} seconds",
            )

    def sideband_progress(self, string: str) -> None:
        self._check_timeout()

    def transfer_progress(self, stats: Any) -> None:
        self._check_timeout()

    def push_transfer_progress(
        self, objects_pushed: int, total_objects: int, bytes_pushed: int
    ) -> None:
        self._check_timeout()

    def push_update_reference(self, refname: str, message: str | None) -> None:
        self._check_timeout()
        if message:
            self.rejections.append((refname, message))


def _git_error(command: Sequence[str], exc: BaseException) -> GitCommandError:
    detail = str(exc).strip() or type(exc).__name__
    return GitCommandError(command, 1, detail)


def _validate_commit_id(commit: str) -> str:
    """Only object ids are accepted where a revision is expected."""

    if not isinstance(commit, str) or not 7 <= len(commit) <= 64:
        raise InvalidSkillPackage("commit must be a hexadecimal object id")
    if any(char not in _HEX for char in commit):
        raise InvalidSkillPackage("commit must be a hexadecimal object id")
    return commit.lower()


def _validate_path_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidSkillPackage(f"{label} must be a non-empty string")
    if value in (".", ".."):
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    if "/" in value or "\\" in value:
        raise InvalidSkillPackage(f"{label} must be one path segment: {value!r}")
    if value.startswith("-") or value.endswith(".") or value.endswith(".lock"):
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidSkillPackage(f"{label} contains control characters")
    if any(char in value for char in '*?<>|:"'):
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    return value


def _validate_ref_segment(value: str, label: str) -> str:
    _validate_path_segment(value, label)
    if any(char in value for char in _REF_FORBIDDEN):
        raise InvalidSkillPackage(f"invalid {label} in a Git ref: {value!r}")
    if ".." in value or "@{" in value:
        raise InvalidSkillPackage(f"invalid {label} in a Git ref: {value!r}")
    return value


def _validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise InvalidSkillPackage("Git ref must be a non-empty string")
    if ref == "HEAD":
        return ref
    if not ref.startswith("refs/"):
        raise InvalidSkillPackage(f"Git ref must be fully qualified: {ref!r}")
    if ref.endswith("/") or ref.endswith(".") or "//" in ref or ".." in ref:
        raise InvalidSkillPackage(f"invalid Git ref: {ref!r}")
    if "@{" in ref:
        raise InvalidSkillPackage(f"invalid Git ref: {ref!r}")
    for part in ref.split("/"):
        _validate_ref_segment(part, "Git ref segment")
    return ref


def _validate_message(message: str) -> str:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("commit message must be non-empty")
    if "\x00" in message:
        raise ValueError("commit message must not contain NUL")
    return message


def _source_directory(source_path: str) -> str:
    """Return the directory for a tree path, accepting SKILL.md itself.

    Task 7 stores the release path as public/<name>/SKILL.md while Task 5's
    publish API names a directory.  Both are unambiguous, so normalise the
    documented file form to its containing skill directory.
    """

    path = validate_skill_path(source_path)
    if PurePosixPath(path).name == _SKILL_FILE:
        parent = str(PurePosixPath(path).parent)
        if parent in ("", "."):
            raise InvalidSkillPackage("a skill path must have a containing directory")
        return parent
    return path


def _safe_tree_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise InvalidSkillPackage("Git tree entry has an invalid name")
    if "\\" in name:
        raise InvalidSkillPackage(f"Git tree entry uses a backslash: {name!r}")
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise InvalidSkillPackage(f"Git tree entry is absolute: {name!r}")
    raw = name[:-1] if name.endswith("/") else name
    parts = tuple(raw.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise InvalidSkillPackage(f"Git tree entry escapes its root: {name!r}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in part):
            raise InvalidSkillPackage(f"Git tree entry has control characters: {name!r}")
        if ":" in part:
            raise InvalidSkillPackage(f"Git tree entry uses a drive or stream: {name!r}")
    return parts


def _prepare_directory(root: Path, parts: Sequence[str]) -> Path:
    current = root
    for part in parts:
        if part in ("", ".", ".."):
            raise InvalidSkillPackage(f"invalid directory segment: {part!r}")
        current = current / part
        if current.is_symlink():
            raise InvalidSkillPackage(f"refusing to follow symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise InvalidSkillPackage(f"path component is not a directory: {current}")
        else:
            current.mkdir()
    return current


def _tree_mode(executable: bool) -> int:
    return 0o100755 if executable else 0o100644


def _build_file_tree(repo: pygit2.Repository, files: Sequence[tuple[str, bytes, bool]]) -> Any:
    """Build the tree for one exact directory without touching a worktree."""

    builder = repo.TreeBuilder()
    directories: dict[str, list[tuple[str, bytes, bool]]] = {}
    file_names: set[str] = set()
    for path, data, executable in files:
        head, separator, rest = path.partition("/")
        if not head:
            raise InvalidSkillPackage("skill file path must be non-empty")
        if separator:
            if head in file_names:
                raise InvalidSkillPackage(f"path is both a file and a directory: {head!r}")
            directories.setdefault(head, []).append((rest, data, executable))
        else:
            if head in file_names:
                raise InvalidSkillPackage(f"duplicate skill path: {path!r}")
            if head in directories:
                raise InvalidSkillPackage(f"path is both a file and a directory: {head!r}")
            file_names.add(head)
            builder.insert(head, repo.create_blob(data), _tree_mode(executable))

    for name, children in directories.items():
        if name in file_names:
            raise InvalidSkillPackage(f"path is both a file and a directory: {name!r}")
        builder.insert(name, _build_file_tree(repo, children), _TREE_MODE)
    return builder.write()


def _replace_tree_directory(
    repo: pygit2.Repository,
    base_tree: pygit2.Tree,
    parts: Sequence[str],
    new_tree: Any,
) -> Any:
    """Replace one directory subtree while preserving every sibling entry."""

    if not parts:
        return new_tree
    builder = repo.TreeBuilder(base_tree)
    name = parts[0]
    if len(parts) == 1:
        builder.insert(name, new_tree, _TREE_MODE)
        return builder.write()

    try:
        entry = base_tree[name]
    except KeyError:
        child = repo.TreeBuilder().write()
    else:
        if int(entry.filemode) != _TREE_MODE:
            raise InvalidSkillPackage(f"tree path component is not a directory: {name!r}")
        child = entry.id
    child_tree = repo[child]
    if not isinstance(child_tree, pygit2.Tree):
        raise InvalidSkillPackage(f"tree path component is not a tree: {name!r}")
    replacement = _replace_tree_directory(repo, child_tree, parts[1:], new_tree)
    builder.insert(name, replacement, _TREE_MODE)
    return builder.write()


class SkillGitRepository:
    """A restricted wrapper around one bare skill repository mirror."""

    def __init__(self, config: SkillGitConfig, cache_dir: Path) -> None:
        if not config.repository.strip():
            raise ValueError("skill Git repository is not configured")
        self.config = config
        self.cache_dir = Path(cache_dir)
        repository = config.repository.strip()
        repo_id = hashlib.sha256(repository.encode("utf-8")).hexdigest()
        self.repo_id = repo_id
        self.mirror_path = self.cache_dir / "repos" / f"{repo_id}.git"
        self._timeout = float(config.command_timeout_seconds)
        self._ready_lock = asyncio.Lock()
        self._signature = pygit2.Signature(SERVICE_NAME, SERVICE_EMAIL)

    def _repo(self) -> pygit2.Repository:
        try:
            return pygit2.Repository(str(self.mirror_path))
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "open", str(self.mirror_path)), exc) from exc

    def _remote(self, repo: pygit2.Repository) -> pygit2.Remote:
        try:
            return repo.remotes["origin"]
        except (KeyError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "remote", "origin"), exc) from exc

    def _ssh_username(self) -> str:
        repository = self.config.repository.strip()
        if "://" in repository:
            try:
                return urlsplit(repository).username or "git"
            except ValueError:
                return "git"
        return "git"

    def _callbacks(self) -> _RemoteCallbacks:
        credentials: Any | None = None
        if self.config.ssh_key_path:
            private = Path(self.config.ssh_key_path)
            public = Path(str(private) + ".pub")
            credentials = pygit2.Keypair(
                self._ssh_username(),
                str(public) if public.exists() else None,
                str(private),
                None,
            )
        return _RemoteCallbacks(timeout=self._timeout, credentials=credentials)

    def _fetch_all_heads(self, repo: pygit2.Repository) -> None:
        try:
            remote = self._remote(repo)
            remote.fetch(
                ["+refs/heads/*:refs/heads/*"],
                callbacks=self._callbacks(),
            )
        except GitCommandError:
            raise
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "fetch", "refs/heads/*"), exc) from exc

    def _commit_or_none(self, repo: pygit2.Repository, commit: str) -> pygit2.Commit | None:
        try:
            obj = repo.revparse_single(commit)
        except (KeyError, ValueError, pygit2.GitError):
            return None
        return obj if isinstance(obj, pygit2.Commit) else None

    def _commit_or_raise(self, repo: pygit2.Repository, commit: str) -> pygit2.Commit:
        found = self._commit_or_none(repo, commit)
        if found is None:
            raise GitCommandError(
                ("pygit2", "rev-parse", commit),
                1,
                f"commit not found: {commit}",
            )
        return found

    def _ensure_commit_available_sync(self, commit: str) -> None:
        repo = self._repo()
        if self._commit_or_none(repo, commit) is not None:
            return
        self._fetch_all_heads(repo)
        if self._commit_or_none(repo, commit) is None:
            raise GitCommandError(
                ("pygit2", "fetch", commit),
                1,
                f"commit not found after fetch: {commit}",
            )

    async def ensure_ready(self) -> None:
        async with self._ready_lock:
            await asyncio.to_thread(self._ensure_ready_sync)

    def _ensure_ready_sync(self) -> None:
        if self.mirror_path.exists():
            if not self.mirror_path.is_dir() or self.mirror_path.is_symlink():
                raise GitCommandError(
                    ("pygit2", "open", str(self.mirror_path)),
                    1,
                    f"mirror path is not a directory: {self.mirror_path}",
                )
            repo = self._repo()
            if not repo.is_bare:
                raise GitCommandError(
                    ("pygit2", "open", str(self.mirror_path)),
                    1,
                    "mirror is not a bare repository",
                )
            return

        self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            repo = pygit2.clone_repository(
                self.config.repository.strip(),
                str(self.mirror_path),
                bare=True,
                callbacks=self._callbacks(),
            )
            self._fetch_all_heads(repo)
        except Exception as exc:
            shutil.rmtree(self.mirror_path, ignore_errors=True)
            if isinstance(exc, GitCommandError):
                raise
            raise _git_error(
                ("pygit2", "clone", self.config.repository.strip()), exc
            ) from exc

    async def _fetch_ref(self, remote_ref: str, local_ref: str) -> bool:
        _validate_ref(remote_ref)
        _validate_ref(local_ref)
        return await asyncio.to_thread(self._fetch_ref_sync, remote_ref, local_ref)

    def _fetch_ref_sync(self, remote_ref: str, local_ref: str) -> bool:
        repo = self._repo()
        try:
            remote = self._remote(repo)
            heads = remote.list_heads(callbacks=self._callbacks())
        except GitCommandError:
            raise
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "list-heads", remote_ref), exc) from exc

        if remote_ref not in {head.name for head in heads}:
            try:
                repo.references.delete(local_ref)
            except (KeyError, pygit2.GitError):
                pass
            return False

        try:
            remote.fetch([f"+{remote_ref}:{local_ref}"], callbacks=self._callbacks())
            repo.lookup_reference(local_ref)
        except GitCommandError:
            raise
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "fetch", remote_ref), exc) from exc
        return True

    async def _ensure_commit_available(self, commit: str) -> None:
        _validate_commit_id(commit)
        await asyncio.to_thread(self._ensure_commit_available_sync, commit)

    async def _empty_base_commit(self) -> str:
        return await asyncio.to_thread(self._empty_base_commit_sync)

    def _empty_base_commit_sync(self) -> str:
        repo = self._repo()
        tree = repo.TreeBuilder().write()
        ref = f"refs/myflow/empty/{uuid.uuid4().hex}"
        try:
            commit = repo.create_commit(
                ref,
                self._signature,
                self._signature,
                "myflow: initialize skill draft history",
                tree,
                [],
            )
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "commit-tree", ref), exc) from exc
        finally:
            try:
                repo.references.delete(ref)
            except (KeyError, pygit2.GitError):
                pass
        return str(commit)

    async def _commit_and_push(
        self,
        *,
        base_commit: str,
        directory: str,
        files: Sequence[tuple[str, bytes, bool]],
        message: str,
        ref: str,
    ) -> str:
        return await asyncio.to_thread(
            self._commit_and_push_sync,
            base_commit,
            directory,
            files,
            message,
            ref,
        )

    def _commit_and_push_sync(
        self,
        base_commit: str,
        directory: str,
        files: Sequence[tuple[str, bytes, bool]],
        message: str,
        ref: str,
    ) -> str:
        repo = self._repo()
        base = self._commit_or_raise(repo, base_commit)
        try:
            file_tree = _build_file_tree(repo, files)
            new_tree = _replace_tree_directory(
                repo,
                base.tree,
                tuple(directory.split("/")),
                file_tree,
            )
            if new_tree == base.tree.id:
                commit_id = base.id
            else:
                commit_id = repo.create_commit(
                    ref,
                    self._signature,
                    self._signature,
                    message,
                    new_tree,
                    [base.id],
                )
            if self._reference_target(repo, ref) is None:
                repo.references.create(ref, commit_id, force=True)
            remote = self._remote(repo)
            callbacks = self._callbacks()
            remote.push([f"{ref}:{ref}"], callbacks=callbacks)
            if callbacks.rejections:
                detail = "; ".join(
                    f"{name}: {reason}" for name, reason in callbacks.rejections
                )
                raise GitCommandError(("pygit2", "push", ref), 1, detail)
        except GitCommandError:
            raise
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "commit", ref), exc) from exc
        return str(commit_id)

    def _reference_target(self, repo: pygit2.Repository, ref: str) -> Any | None:
        if ref == "HEAD":
            try:
                return repo.head.target
            except (KeyError, pygit2.GitError):
                return None
        try:
            return repo.lookup_reference(ref).target
        except (KeyError, pygit2.GitError):
            return None

    async def save_draft(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        files: Sequence[StagedFile],
        message: str,
    ) -> GitCommit:
        owner = _validate_ref_segment(owner_user_id, "owner_user_id")
        skill = _validate_path_segment(skill_id, "skill_id")
        message = _validate_message(message)
        normalized: list[tuple[str, bytes, bool]] = []
        seen: set[str] = set()
        for staged in files:
            if not isinstance(staged, StagedFile):
                raise InvalidSkillPackage("draft entries must be StagedFile values")
            rel = validate_skill_path(staged.path)
            if rel in seen:
                raise InvalidSkillPackage(f"duplicate skill path: {rel!r}")
            if not isinstance(staged.content, bytes):
                raise InvalidSkillPackage(f"file content must be bytes: {rel!r}")
            seen.add(rel)
            normalized.append((rel, staged.content, staged.mode == "executable"))
        if not normalized:
            raise InvalidSkillPackage("a saved draft must contain at least one file")

        prefix = self.config.draft_ref_prefix.rstrip("/")
        ref = _validate_ref(f"{prefix}/{owner}")
        _validate_ref(prefix)
        path = f"users/{owner}/{skill}"
        await self.ensure_ready()
        exists = await self._fetch_ref(ref, ref)
        base = await self.head(ref) if exists else await self._empty_base_commit()
        commit = await self._commit_and_push(
            base_commit=base,
            directory=path,
            files=normalized,
            message=message,
            ref=ref,
        )
        return GitCommit(commit=commit, ref=ref, path=path)

    async def publish_public(
        self,
        *,
        public_name: str,
        source_commit: str,
        source_path: str,
        message: str,
    ) -> GitCommit:
        name = _validate_path_segment(public_name, "public_name")
        source = _validate_commit_id(source_commit)
        source_dir = _source_directory(source_path)
        message = _validate_message(message)
        branch = _validate_ref_segment(self.config.public_branch, "public_branch")
        ref = _validate_ref(f"refs/heads/{branch}")
        path = f"public/{name}"

        await self.ensure_ready()
        if not await self._fetch_ref(ref, ref):
            raise GitCommandError(
                ("pygit2", "fetch", ref), 2, f"public branch not found: {ref}"
            )
        await self._ensure_commit_available(source)
        entries = await self._tree_entries(source, source_dir)
        if not entries:
            raise InvalidSkillPackage(f"source tree has no files: {source}:{source_dir}")
        payload: list[tuple[str, bytes, bool]] = []
        for entry in entries:
            rel = entry.path
            if rel == source_dir:
                rel = PurePosixPath(source_dir).name
            elif rel.startswith(source_dir + "/"):
                rel = rel[len(source_dir) + 1 :]
            else:
                raise InvalidSkillPackage(f"tree entry escapes its source: {entry.path!r}")
            data = await self._read_blob(entry.oid)
            payload.append((rel, data, entry.mode == "100755"))

        base = await self.head(ref)
        commit = await self._commit_and_push(
            base_commit=base,
            directory=path,
            files=payload,
            message=message,
            ref=ref,
        )
        return GitCommit(commit=commit, ref=ref, path=path)

    async def head(self, ref: str) -> str:
        _validate_ref(ref)
        return await asyncio.to_thread(self._head_sync, ref)

    def _head_sync(self, ref: str) -> str:
        repo = self._repo()
        try:
            if ref == "HEAD":
                target = repo.head.target
            else:
                target = repo.lookup_reference(ref).target
            obj = repo[target]
            if not isinstance(obj, pygit2.Commit):
                raise GitCommandError(("pygit2", "rev-parse", ref), 1, "ref is not a commit")
        except GitCommandError:
            raise
        except (KeyError, OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "rev-parse", ref), exc) from exc
        return str(target)

    async def list_tree(self, commit: str, path: str) -> list[str]:
        source = _validate_commit_id(commit)
        directory = validate_skill_path(path)
        entries = await self._tree_entries(source, directory)
        relative: list[str] = []
        for entry in entries:
            if entry.path == directory:
                relative.append(PurePosixPath(directory).name)
            elif entry.path.startswith(directory + "/"):
                relative.append(entry.path[len(directory) + 1 :])
            else:
                raise InvalidSkillPackage(f"tree entry escapes its path: {entry.path!r}")
        return relative

    async def package_sha256(self, commit: str, path: str) -> str:
        """Hash one skill directory exactly as build_package hashes its files."""

        source = _validate_commit_id(commit)
        directory = _source_directory(path)
        entries = await self._tree_entries(source, directory)
        if not entries:
            raise InvalidSkillPackage(
                f"source tree has no files: {source}:{directory}"
            )

        relative: list[tuple[str, _TreeEntry]] = []
        for entry in entries:
            if entry.path == directory:
                rel = PurePosixPath(directory).name
            elif entry.path.startswith(directory + "/"):
                rel = entry.path[len(directory) + 1 :]
            else:
                raise InvalidSkillPackage(
                    f"tree entry escapes its path: {entry.path!r}"
                )
            relative.append((rel, entry))

        relative.sort(key=lambda item: item[0])
        if _SKILL_FILE not in {rel for rel, _entry in relative}:
            raise InvalidSkillPackage(f"package is missing {_SKILL_FILE}")

        hasher = hashlib.sha256()
        for rel, entry in relative:
            content = await self._read_blob(entry.oid)
            mode = "executable" if entry.mode == "100755" else "text"
            hasher.update(rel.encode("utf-8"))
            hasher.update(_NUL)
            hasher.update(mode.encode("utf-8"))
            hasher.update(_NUL)
            hasher.update(content)
            hasher.update(_NUL)
        return hasher.hexdigest()

    async def read_file(self, commit: str, path: str) -> bytes:
        source = _validate_commit_id(commit)
        relative = validate_skill_path(path)
        return await asyncio.to_thread(self._read_file_sync, source, relative)

    def _read_file_sync(self, source: str, relative: str) -> bytes:
        repo = self._repo()
        commit = self._commit_or_raise(repo, source)
        try:
            node: Any = commit.tree
            parts = relative.split("/")
            for index, part in enumerate(parts):
                entry = node[part]
                if index == len(parts) - 1:
                    if int(entry.filemode) not in _BLOB_MODES:
                        raise KeyError(relative)
                    blob = repo[entry.id]
                    if not isinstance(blob, pygit2.Blob):
                        raise KeyError(relative)
                    return blob.data
                if int(entry.filemode) != _TREE_MODE:
                    raise KeyError(relative)
                node = repo[entry.id]
        except KeyError as exc:
            raise GitCommandError(
                ("pygit2", "show", f"{source}:{relative}"),
                1,
                f"path not found: {relative}",
            ) from exc
        except (OSError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "show", f"{source}:{relative}"), exc) from exc
        raise GitCommandError(
            ("pygit2", "show", f"{source}:{relative}"),
            1,
            f"path not found: {relative}",
        )

    async def materialize(self, commit: str, destination: Path) -> None:
        source = _validate_commit_id(commit)
        await self.ensure_ready()
        await self._ensure_commit_available(source)
        await asyncio.to_thread(self._materialize_sync, source, Path(destination))

    async def _tree_entries(self, commit: str, path: str) -> list[_TreeEntry]:
        return await asyncio.to_thread(self._tree_entries_sync, commit, path)

    def _tree_entries_sync(self, commit: str, path: str) -> list[_TreeEntry]:
        repo = self._repo()
        root = self._commit_or_raise(repo, commit)
        parts = validate_skill_path(path).split("/")
        node: Any = root.tree
        for index, part in enumerate(parts):
            try:
                entry = node[part]
            except KeyError:
                return []
            mode = int(entry.filemode)
            if index == len(parts) - 1:
                if mode in _BLOB_MODES:
                    return [_TreeEntry(path=path, mode=f"{mode:o}", oid=str(entry.id))]
                if mode != _TREE_MODE:
                    raise InvalidSkillPackage(
                        f"Git tree contains a link or special file: {path!r}"
                    )
                node = repo[entry.id]
                break
            if mode != _TREE_MODE:
                return []
            node = repo[entry.id]

        entries: list[_TreeEntry] = []

        def walk(tree: pygit2.Tree, prefix: str) -> None:
            for item in tree:
                name = item.name
                full = f"{prefix}/{name}" if prefix else name
                mode = int(item.filemode)
                if mode == _TREE_MODE:
                    child = repo[item.id]
                    if not isinstance(child, pygit2.Tree):
                        raise InvalidSkillPackage(f"Git tree contains an invalid tree: {full!r}")
                    walk(child, full)
                    continue
                if mode not in _BLOB_MODES:
                    raise InvalidSkillPackage(
                        f"Git tree contains a link or special file: {full!r}"
                    )
                validated = validate_skill_path(full)
                entries.append(_TreeEntry(path=validated, mode=f"{mode:o}", oid=str(item.id)))

        walk(node, path)
        entries.sort(key=lambda entry: entry.path)
        return entries

    async def _read_blob(self, oid: str) -> bytes:
        _validate_commit_id(oid)
        return await asyncio.to_thread(self._read_blob_sync, oid)

    def _read_blob_sync(self, oid: str) -> bytes:
        repo = self._repo()
        try:
            obj = repo.revparse_single(oid)
        except (KeyError, ValueError, pygit2.GitError) as exc:
            raise _git_error(("pygit2", "cat-file", oid), exc) from exc
        if not isinstance(obj, pygit2.Blob):
            raise InvalidSkillPackage(f"Git object is not a blob: {oid!r}")
        return obj.data

    def _materialize_sync(self, commit: str, destination: Path) -> None:
        if destination.is_symlink():
            raise InvalidSkillPackage(f"materialize destination is a symlink: {destination}")
        parent = destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                raise InvalidSkillPackage(
                    f"materialize destination must be empty: {destination}"
                )

        repo = self._repo()
        root = self._commit_or_raise(repo, commit)
        temporary = Path(tempfile.mkdtemp(prefix=".myflow-extract-", dir=str(parent)))
        try:
            entries = self._all_tree_entries(repo, root.tree)
            if len(entries) > MAX_ARCHIVE_FILES:
                raise InvalidSkillPackage("Git tree contains too many files")
            total = 0
            for entry in entries:
                parts = _safe_tree_parts(entry.path)
                data = self._read_blob_sync(entry.oid)
                if len(data) > MAX_FILE_BYTES:
                    raise InvalidSkillPackage(f"Git tree file is too large: {entry.path!r}")
                total += len(data)
                if total > MAX_ARCHIVE_BYTES:
                    raise InvalidSkillPackage("Git tree exceeds the extracted size limit")
                holder = _prepare_directory(temporary, parts[:-1])
                target = holder / parts[-1]
                if target.exists() or target.is_symlink():
                    raise InvalidSkillPackage(f"duplicate Git tree entry: {entry.path!r}")
                with target.open("xb") as handle:
                    handle.write(data)
                os.chmod(target, 0o755 if entry.mode == "100755" else 0o644)
            if destination.exists():
                destination.rmdir()
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _all_tree_entries(self, repo: pygit2.Repository, tree: pygit2.Tree) -> list[_TreeEntry]:
        entries: list[_TreeEntry] = []

        def walk(current: pygit2.Tree, prefix: str) -> None:
            for item in current:
                name = item.name
                full = f"{prefix}/{name}" if prefix else name
                mode = int(item.filemode)
                if mode == _TREE_MODE:
                    child = repo[item.id]
                    if not isinstance(child, pygit2.Tree):
                        raise InvalidSkillPackage(f"Git tree contains an invalid tree: {full!r}")
                    walk(child, full)
                    continue
                if mode not in _BLOB_MODES:
                    raise InvalidSkillPackage(
                        f"Git tree contains a link or special file: {full!r}"
                    )
                _safe_tree_parts(full)
                entries.append(_TreeEntry(path=full, mode=f"{mode:o}", oid=str(item.id)))

        walk(tree, "")
        entries.sort(key=lambda entry: entry.path)
        return entries


__all__ = ["GitCommandError", "GitCommit", "SkillGitRepository"]
