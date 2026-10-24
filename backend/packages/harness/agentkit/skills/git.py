"""Restricted Git writer for the skill catalog.

This module is the only place in the application allowed to mutate Git.  Every
command is an argument vector executed without a shell, under a fixed service
identity, with hooks and interactive prompts disabled.  Content entering or
leaving Git is validated as an exact file set; archives are unpacked by a small
strict extractor rather than by tarfile.extractall.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentkit.config.skills_config import SkillGitConfig
from agentkit.skills.domain import InvalidSkillPackage, StagedFile
from agentkit.skills.package import validate_skill_path

SERVICE_NAME = "MyFlow Skills Service"
SERVICE_EMAIL = "skills-service@myflow.local"

# The archive limit bounds one materialized commit.  It is deliberately higher
# than one skill package because a public commit contains every active skill.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
MAX_FILE_BYTES = 16 * 1024 * 1024

_HEX = frozenset("0123456789abcdefABCDEF")
_REF_FORBIDDEN = frozenset(" ~^:?*[\\")
_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True, slots=True)
class GitCommit:
    """One commit created by this service."""

    commit: str
    ref: str
    path: str


class GitCommandError(RuntimeError):
    """A git subprocess failed; stderr is retained for diagnostics."""

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        shown = " ".join(self.command)
        detail = stderr.strip() or "no stderr"
        super().__init__(f"git command failed ({returncode}): {shown}: {detail}")


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    oid: str


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


def _safe_tar_parts(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise InvalidSkillPackage("archive entry has an invalid name")
    if "\\" in name:
        raise InvalidSkillPackage(f"archive entry uses a backslash: {name!r}")
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise InvalidSkillPackage(f"archive entry is absolute: {name!r}")
    raw = name[:-1] if name.endswith("/") else name
    parts = tuple(raw.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise InvalidSkillPackage(f"archive entry escapes its root: {name!r}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in part):
            raise InvalidSkillPackage(f"archive entry has control characters: {name!r}")
        if ":" in part:
            raise InvalidSkillPackage(f"archive entry uses a drive or stream: {name!r}")
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


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        if not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)


def _write_exact_directory(
    root: Path,
    directory: str,
    files: Sequence[tuple[str, bytes, str]],
) -> None:
    """Replace one directory with exactly the supplied relative files."""

    dir_path = validate_skill_path(directory)
    parent_parts = dir_path.split("/")[:-1]
    parent = _prepare_directory(root, parent_parts)
    target = parent / dir_path.split("/")[-1]
    _remove_path(target)
    target.mkdir()

    seen: set[str] = set()
    for relative, data, mode in files:
        rel = validate_skill_path(relative)
        if rel in seen:
            raise InvalidSkillPackage(f"duplicate skill path: {rel!r}")
        seen.add(rel)
        if not isinstance(data, bytes):
            raise InvalidSkillPackage(f"file content must be bytes: {rel!r}")
        parts = rel.split("/")
        holder = _prepare_directory(target, parts[:-1])
        destination = holder / parts[-1]
        if destination.is_symlink() or destination.exists():
            raise InvalidSkillPackage(f"duplicate skill path: {rel!r}")
        with destination.open("xb") as handle:
            handle.write(data)
        os.chmod(destination, 0o755 if mode == "executable" else 0o644)


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
        self._hooks_dir = self.cache_dir / "hooks"
        self._hooks_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = config.command_timeout_seconds
        self._ready_lock = asyncio.Lock()
        self._env = self._build_environment()
        self._prefix = (
            "git",
            "--no-pager",
            "--literal-pathspecs",
            "-c",
            f"core.hooksPath={self._hooks_dir}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            "advice.detachedHead=false",
            "-c",
            "commit.gpgsign=false",
        )

    # ------------------------------------------------------------------
    # subprocess plumbing
    # ------------------------------------------------------------------

    def _build_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_HOOKS_PATH": str(self._hooks_dir),
                "GIT_AUTHOR_NAME": SERVICE_NAME,
                "GIT_AUTHOR_EMAIL": SERVICE_EMAIL,
                "GIT_COMMITTER_NAME": SERVICE_NAME,
                "GIT_COMMITTER_EMAIL": SERVICE_EMAIL,
                "GIT_PAGER": "cat",
                "PAGER": "cat",
            }
        )
        ssh = ["ssh", "-o", "BatchMode=yes"]
        if self.config.ssh_key_path:
            ssh.extend(["-i", self.config.ssh_key_path])
        if self.config.known_hosts_path:
            ssh.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self.config.known_hosts_path}",
                    "-o",
                    "StrictHostKeyChecking=yes",
                ]
            )
        if self.config.ssh_key_path or self.config.known_hosts_path:
            env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(item) for item in ssh)
        return env

    def _command(self, args: Sequence[str]) -> tuple[str, ...]:
        return (*self._prefix, *args)

    def _execute_sync(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None,
        input_bytes: bytes | None = None,
    ) -> tuple[bytes, bytes, int]:
        command = self._command(args)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                timeout=self._timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "replace")
            raise GitCommandError(command, -1, f"timed out: {stderr}") from exc
        except OSError as exc:
            raise GitCommandError(command, 127, str(exc)) from exc
        return completed.stdout or b"", completed.stderr or b"", completed.returncode

    async def _execute(
        self,
        *args: str,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> tuple[bytes, bytes, int]:
        return await asyncio.to_thread(
            self._execute_sync, args, cwd=cwd, input_bytes=input_bytes
        )

    async def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        stdout, stderr, returncode = await self._execute(
            *args, cwd=cwd, input_bytes=input_bytes
        )
        if returncode != 0:
            raise GitCommandError(
                self._command(args),
                returncode,
                stderr.decode("utf-8", "replace"),
            )
        return stdout

    # ------------------------------------------------------------------
    # mirror and worktrees
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        async with self._ready_lock:
            if self.mirror_path.exists():
                if not self.mirror_path.is_dir() or self.mirror_path.is_symlink():
                    raise GitCommandError(
                        self._command(("rev-parse",)),
                        1,
                        f"mirror path is not a directory: {self.mirror_path}",
                    )
                stdout, stderr, returncode = await self._execute(
                    "rev-parse", "--is-bare-repository", cwd=self.mirror_path
                )
                if returncode != 0 or stdout.strip() != b"true":
                    raise GitCommandError(
                        self._command(("rev-parse", "--is-bare-repository")),
                        returncode,
                        stderr.decode("utf-8", "replace") or "not a bare repository",
                    )
                return

            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self._git(
                    "clone",
                    "--mirror",
                    "--no-hardlinks",
                    "--",
                    self.config.repository.strip(),
                    str(self.mirror_path),
                )
            except GitCommandError:
                if self.mirror_path.exists():
                    shutil.rmtree(self.mirror_path, ignore_errors=True)
                raise
            # A mirror clone refuses an explicit push refspec.  The object
            # database is still a complete bare mirror; only the push syntax is
            # relaxed so a worker can publish exactly one draft/public ref.
            await self._git(
                "config", "remote.origin.mirror", "false", cwd=self.mirror_path
            )
            await self._git(
                "config",
                "remote.origin.fetch",
                "+refs/heads/*:refs/heads/*",
                cwd=self.mirror_path,
            )

    async def _fetch_ref(self, remote_ref: str, local_ref: str) -> bool:
        _validate_ref(remote_ref)
        _validate_ref(local_ref)
        stdout, stderr, returncode = await self._execute(
            "ls-remote", "--exit-code", "origin", remote_ref, cwd=self.mirror_path
        )
        if returncode == 2 or (returncode == 0 and not stdout.strip()):
            await self._git("update-ref", "-d", local_ref, cwd=self.mirror_path)
            return False
        if returncode != 0:
            raise GitCommandError(
                self._command(("ls-remote", "--exit-code", "origin", remote_ref)),
                returncode,
                stderr.decode("utf-8", "replace"),
            )
        await self._git(
            "fetch",
            "--no-tags",
            "origin",
            f"+{remote_ref}:{local_ref}",
            cwd=self.mirror_path,
        )
        return True

    async def _ensure_commit_available(self, commit: str) -> None:
        _validate_commit_id(commit)
        _stdout, _stderr, returncode = await self._execute(
            "cat-file", "-e", f"{commit}^{{commit}}", cwd=self.mirror_path
        )
        if returncode == 0:
            return
        await self._git(
            "fetch",
            "--no-tags",
            "origin",
            "+refs/heads/*:refs/heads/*",
            cwd=self.mirror_path,
        )
        await self._git("cat-file", "-e", f"{commit}^{{commit}}", cwd=self.mirror_path)

    async def _empty_base_commit(self) -> str:
        tree = await self._git("mktree", cwd=self.mirror_path, input_bytes=b"")
        commit = await self._git(
            "commit-tree",
            tree.decode("ascii").strip(),
            "-m",
            "myflow: initialize skill draft history",
            cwd=self.mirror_path,
        )
        return commit.decode("ascii").strip()

    @asynccontextmanager
    async def _worktree(self, commit: str) -> AsyncIterator[Path]:
        temporary = tempfile.TemporaryDirectory(prefix="myflow-skill-worktree-")
        worktree = Path(temporary.name) / "tree"
        added = False
        try:
            await self._git(
                "worktree",
                "add",
                "--detach",
                str(worktree),
                commit,
                cwd=self.mirror_path,
            )
            added = True
            yield worktree
        finally:
            if added and worktree.exists():
                await self._execute(
                    "worktree", "remove", "--force", str(worktree), cwd=self.mirror_path
                )
            await self._execute("worktree", "prune", cwd=self.mirror_path)
            temporary.cleanup()

    async def _commit_and_push(
        self,
        worktree: Path,
        *,
        path: str,
        message: str,
        ref: str,
        modes: Mapping[str, bool],
    ) -> str:
        await self._git("add", "--all", "--", path, cwd=worktree)
        # core.fileMode is false on Windows, so preserve the skill's declared
        # executable bit explicitly instead of trusting the checkout.
        for indexed_path in sorted(modes):
            await self._git(
                "update-index",
                "--chmod=+x" if modes[indexed_path] else "--chmod=-x",
                "--",
                indexed_path,
                cwd=worktree,
            )
        status = await self._git("status", "--porcelain", "--", path, cwd=worktree)
        if status.strip():
            await self._git("commit", "-m", message, cwd=worktree)
        commit = (await self._git("rev-parse", "HEAD", cwd=worktree)).decode("ascii").strip()
        await self._git("push", "--porcelain", "origin", f"HEAD:{ref}", cwd=worktree)
        return commit

    # ------------------------------------------------------------------
    # public writes and reads
    # ------------------------------------------------------------------

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
        normalized: list[tuple[str, bytes, str]] = []
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
            normalized.append((rel, staged.content, staged.mode))
        if not normalized:
            raise InvalidSkillPackage("a saved draft must contain at least one file")

        prefix = self.config.draft_ref_prefix.rstrip("/")
        ref = _validate_ref(f"{prefix}/{owner}")
        _validate_ref(prefix)
        path = f"users/{owner}/{skill}"
        await self.ensure_ready()
        exists = await self._fetch_ref(ref, ref)
        base = await self.head(ref) if exists else await self._empty_base_commit()
        async with self._worktree(base) as worktree:
            _write_exact_directory(worktree, path, normalized)
            commit = await self._commit_and_push(
                worktree,
                path=path,
                message=message,
                ref=ref,
                modes={
                    f"{path}/{rel}": mode == "executable"
                    for rel, _data, mode in normalized
                },
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
                self._command(("fetch", "origin", ref)), 2, f"public branch not found: {ref}"
            )
        await self._ensure_commit_available(source)
        entries = await self._tree_entries(source, source_dir)
        if not entries:
            raise InvalidSkillPackage(f"source tree has no files: {source}:{source_dir}")
        payload: list[tuple[str, bytes, str]] = []
        for entry in entries:
            rel = entry.path
            if rel == source_dir:
                rel = PurePosixPath(source_dir).name
            elif rel.startswith(source_dir + "/"):
                rel = rel[len(source_dir) + 1 :]
            else:
                raise InvalidSkillPackage(f"tree entry escapes its source: {entry.path!r}")
            data = await self._read_blob(entry.oid)
            payload.append((rel, data, "executable" if entry.mode == "100755" else "text"))

        base = await self.head(ref)
        async with self._worktree(base) as worktree:
            _write_exact_directory(worktree, path, payload)
            commit = await self._commit_and_push(
                worktree,
                path=path,
                message=message,
                ref=ref,
                modes={
                    f"{path}/{rel}": mode == "executable"
                    for rel, _data, mode in payload
                },
            )
        return GitCommit(commit=commit, ref=ref, path=path)

    async def head(self, ref: str) -> str:
        _validate_ref(ref)
        stdout = await self._git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
            cwd=self.mirror_path,
        )
        return stdout.decode("ascii").strip()

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

    async def read_file(self, commit: str, path: str) -> bytes:
        source = _validate_commit_id(commit)
        relative = validate_skill_path(path)
        return await self._git(
            "show",
            f"{source}:{relative}",
            cwd=self.mirror_path,
        )

    async def materialize(self, commit: str, destination: Path) -> None:
        source = _validate_commit_id(commit)
        await self.ensure_ready()
        await self._ensure_commit_available(source)
        await asyncio.to_thread(self._materialize_sync, source, Path(destination))

    # ------------------------------------------------------------------
    # tree and archive internals
    # ------------------------------------------------------------------

    async def _tree_entries(self, commit: str, path: str) -> list[_TreeEntry]:
        output = await self._git(
            "ls-tree",
            "-r",
            "-z",
            commit,
            "--",
            path,
            cwd=self.mirror_path,
        )
        entries: list[_TreeEntry] = []
        for record in output.split(b"\x00"):
            if not record:
                continue
            try:
                metadata, raw_path = record.split(b"\t", 1)
                mode, kind, oid = metadata.split(b" ")
                full_path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise InvalidSkillPackage("Git tree contains an invalid path") from exc
            if kind != b"blob":
                raise InvalidSkillPackage(f"Git tree contains a non-blob: {full_path!r}")
            if mode not in (b"100644", b"100755"):
                raise InvalidSkillPackage(
                    f"Git tree contains a link or special file: {full_path!r}"
                )
            validated = validate_skill_path(full_path)
            if validated != path and not validated.startswith(path + "/"):
                raise InvalidSkillPackage(f"Git tree entry escapes its path: {validated!r}")
            entries.append(
                _TreeEntry(
                    path=validated,
                    mode=mode.decode("ascii"),
                    oid=oid.decode("ascii"),
                )
            )
        entries.sort(key=lambda entry: entry.path)
        return entries

    async def _read_blob(self, oid: str) -> bytes:
        _validate_commit_id(oid)
        return await self._git("cat-file", "blob", oid, cwd=self.mirror_path)

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

        descriptor, archive_name = tempfile.mkstemp(
            prefix=".myflow-archive-", suffix=".tar", dir=str(parent)
        )
        os.close(descriptor)
        archive = Path(archive_name)
        temporary = Path(tempfile.mkdtemp(prefix=".myflow-extract-", dir=str(parent)))
        try:
            command = self._command(
                (
                    "archive",
                    "--format=tar",
                    "--output",
                    str(archive),
                    commit,
                )
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.mirror_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._env,
                    timeout=self._timeout,
                    shell=False,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise GitCommandError(command, -1, "git archive timed out") from exc
            except OSError as exc:
                raise GitCommandError(command, 127, str(exc)) from exc
            if completed.returncode != 0:
                raise GitCommandError(
                    command,
                    completed.returncode,
                    completed.stderr.decode("utf-8", "replace"),
                )
            if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise InvalidSkillPackage("Git archive exceeds the size limit")
            self._extract_archive(archive, temporary)
            if destination.exists():
                destination.rmdir()
            os.replace(temporary, destination)
        finally:
            archive.unlink(missing_ok=True)
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _extract_archive(self, archive: Path, root: Path) -> None:
        files = 0
        entries = 0
        total = 0
        seen: set[str] = set()
        with tarfile.open(archive, mode="r:") as tar:
            for member in tar:
                entries += 1
                if entries > MAX_ARCHIVE_FILES * 2:
                    raise InvalidSkillPackage("Git archive contains too many entries")
                parts = _safe_tar_parts(member.name)
                key = "/".join(parts)
                if key in seen:
                    raise InvalidSkillPackage(f"duplicate archive entry: {member.name!r}")
                seen.add(key)
                if member.isdir():
                    _prepare_directory(root, parts)
                    continue
                if not member.isreg():
                    raise InvalidSkillPackage(
                        f"archive entry is a link or device: {member.name!r}"
                    )
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise InvalidSkillPackage(f"archive file is too large: {member.name!r}")
                files += 1
                if files > MAX_ARCHIVE_FILES:
                    raise InvalidSkillPackage("Git archive contains too many files")
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise InvalidSkillPackage("Git archive exceeds the extracted size limit")

                holder = _prepare_directory(root, parts[:-1])
                target = holder / parts[-1]
                if target.exists() or target.is_symlink():
                    raise InvalidSkillPackage(f"duplicate archive entry: {member.name!r}")
                source = tar.extractfile(member)
                if source is None:
                    raise InvalidSkillPackage(f"archive entry has no body: {member.name!r}")
                with target.open("xb") as handle:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise InvalidSkillPackage(
                                f"archive entry is truncated: {member.name!r}"
                            )
                        handle.write(chunk)
                        remaining -= len(chunk)
                executable = bool(member.mode & 0o111)
                os.chmod(target, 0o755 if executable else 0o644)


__all__ = ["GitCommandError", "GitCommit", "SkillGitRepository"]
