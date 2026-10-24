"""Immutable commit cache and read-only active skill trees.

Git objects are immutable, so a commit can be exported once into
cache_dir/commits/<commit> and served without touching a working tree.  Active
trees are rebuilt from those caches into temporary sibling directories and
swapped into place, so a reader never sees a half-copied skill.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillScope,
    VisibleSkill,
)
from agentkit.skills.git import SkillGitRepository
from agentkit.skills.package import validate_skill_path

_HEX = frozenset("0123456789abcdefABCDEF")
_SKILL_FILE = "SKILL.md"


def _validate_commit_id(commit: str) -> str:
    if not isinstance(commit, str) or not 7 <= len(commit) <= 64:
        raise InvalidSkillPackage("commit must be a hexadecimal object id")
    if any(char not in _HEX for char in commit):
        raise InvalidSkillPackage("commit must be a hexadecimal object id")
    return commit.lower()


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidSkillPackage(f"{label} must be a non-empty string")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    if value.startswith("-") or value.endswith(".") or value.endswith(".lock"):
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidSkillPackage(f"{label} contains control characters")
    if any(char in value for char in '*?<>|:"'):
        raise InvalidSkillPackage(f"invalid {label}: {value!r}")
    return value


def _copy_tree_safe(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise InvalidSkillPackage(f"cached skill is not a directory: {source}")
    destination.mkdir()
    with os.scandir(source) as entries:
        for entry in entries:
            target = destination / entry.name
            if entry.is_symlink():
                raise InvalidSkillPackage(f"cached skill contains a symlink: {entry.path}")
            if entry.is_dir(follow_symlinks=False):
                _copy_tree_safe(Path(entry.path), target)
            elif entry.is_file(follow_symlinks=False):
                shutil.copyfile(entry.path, target, follow_symlinks=False)
                executable = bool(entry.stat(follow_symlinks=False).st_mode & 0o100)
                os.chmod(target, 0o755 if executable else 0o644)
            else:
                raise InvalidSkillPackage(
                    f"cached skill contains a special file: {entry.path}"
                )


def _skill_directory(raw_path: str) -> str:
    path = validate_skill_path(raw_path)
    if PurePosixPath(path).name == _SKILL_FILE:
        parent = str(PurePosixPath(path).parent)
        if parent in ("", "."):
            raise InvalidSkillPackage("a skill path must have a containing directory")
        return parent
    return path


class SkillContentStore:
    """Read-only views over immutable Git commits."""

    def __init__(self, cache_dir: Path, git: SkillGitRepository) -> None:
        self.cache_dir = Path(cache_dir)
        self.git = git
        self.commits_dir = self.cache_dir / "commits"
        self.active_dir = self.cache_dir / "active"

    # ------------------------------------------------------------------
    # commit cache
    # ------------------------------------------------------------------

    async def ensure(self, commit: str) -> Path:
        source = _validate_commit_id(commit)
        target = self.commits_dir / source
        if target.is_symlink():
            raise InvalidSkillPackage(f"commit cache path is a symlink: {target}")
        if target.exists():
            if not target.is_dir():
                raise InvalidSkillPackage(f"commit cache path is not a directory: {target}")
            return target

        self.commits_dir.mkdir(parents=True, exist_ok=True)
        # Staging lives beside the cache root, never inside commits/, so a
        # concurrent prune only ever sees finished commit directories and
        # cannot delete a materialization that is still in flight.
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".staging-{source[:12]}-", dir=str(self.cache_dir))
        )
        tree = staging / "tree"
        try:
            await self.git.materialize(source, tree)
            try:
                os.replace(tree, target)
            except OSError:
                if target.is_dir() and not target.is_symlink():
                    return target
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return target

    async def read_text(self, commit: str, path: str) -> str:
        return (await self.read_bytes(commit, path)).decode("utf-8")

    async def read_bytes(self, commit: str, path: str) -> bytes:
        source = _validate_commit_id(commit)
        relative = validate_skill_path(path)
        root = await self.ensure(source)
        target = self._cached_path(root, relative)
        if target.is_symlink() or not target.is_file():
            raise InvalidSkillPackage(f"cached path is not a regular file: {relative!r}")
        return await asyncio.to_thread(target.read_bytes)

    @staticmethod
    def _cached_path(root: Path, relative: str) -> Path:
        current = root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                raise InvalidSkillPackage(f"refusing to follow cached symlink: {relative!r}")
        return current

    # ------------------------------------------------------------------
    # active trees
    # ------------------------------------------------------------------

    async def materialize_public(self, active: Sequence[VisibleSkill]) -> Path:
        specs: list[tuple[str, str, str]] = []
        for visible in active:
            if visible.scope is not SkillScope.PUBLIC or not visible.skill.enabled:
                continue
            release = visible.release
            if release is None or not release.git_commit:
                raise InvalidSkillPackage("public active skill has no release commit")
            name = _validate_name(release.public_name, "public_name")
            specs.append((name, release.git_commit, _skill_directory(release.skill_path)))
        return await self._materialize_active(self.active_dir / "public", specs)

    async def materialize_private(
        self, owner_user_id: str, active: Sequence[VisibleSkill]
    ) -> Path:
        owner = _validate_name(owner_user_id, "owner_user_id")
        specs: list[tuple[str, str, str]] = []
        for visible in active:
            if visible.scope is not SkillScope.PRIVATE:
                continue
            if visible.skill.owner_user_id != owner:
                continue
            version = visible.version
            if not version.draft_commit:
                raise InvalidSkillPackage("private active skill has no draft commit")
            name = _validate_name(visible.skill.name, "skill name")
            specs.append(
                (name, version.draft_commit, _skill_directory(version.skill_path))
            )
        return await self._materialize_active(self.active_dir / "users" / owner, specs)

    def existing_private_owners(self) -> list[str]:
        """已经物化出私有挂载树的所有者（active/users/<owner>）。

        运行时用它找出"这一轮没有私有激活行、但上一轮留了树"的用户并收掉旧树；
        少了这一步，用户删掉最后一个私有技能之后沙箱还在挂一棵不该存在的目录。
        """
        root = self.active_dir / "users"
        if root.is_symlink() or not root.is_dir():
            return []
        with os.scandir(root) as entries:
            return sorted(
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
            )

    async def _materialize_active(
        self,
        target: Path,
        specs: Sequence[tuple[str, str, str]],
    ) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".active-", dir=str(target.parent)))
        seen: set[str] = set()
        try:
            for name, commit, directory in specs:
                if name in seen:
                    raise InvalidSkillPackage(f"duplicate active skill name: {name!r}")
                seen.add(name)
                root = await self.ensure(commit)
                source = self._cached_path(root, directory)
                if source.is_symlink() or not source.is_dir():
                    raise InvalidSkillPackage(
                        f"active skill is not a cached directory: {commit}:{directory}"
                    )
                await asyncio.to_thread(_copy_tree_safe, source, staging / name)
            self._swap_active(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return target

    @staticmethod
    def _swap_active(staging: Path, target: Path) -> None:
        trash: Path | None = None
        if target.is_symlink():
            raise InvalidSkillPackage(f"active tree is a symlink: {target}")
        if target.exists():
            trash = target.parent / f".old-{uuid.uuid4().hex}"
            os.replace(target, trash)
        try:
            os.replace(staging, target)
        except BaseException:
            if trash is not None and trash.exists() and not target.exists():
                os.replace(trash, target)
            raise
        else:
            if trash is not None:
                shutil.rmtree(trash, ignore_errors=True)

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------

    async def prune(self, keep_commits: set[str]) -> int:
        return await asyncio.to_thread(self._prune_sync, set(keep_commits))

    def _prune_sync(self, keep_commits: set[str]) -> int:
        if not self.commits_dir.is_dir() or self.commits_dir.is_symlink():
            return 0
        removed = 0
        with os.scandir(self.commits_dir) as entries:
            for entry in entries:
                if entry.name in keep_commits:
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                shutil.rmtree(entry.path)
                removed += 1
        return removed


__all__ = ["SkillContentStore"]
