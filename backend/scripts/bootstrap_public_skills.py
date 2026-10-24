"""`uv run python -m backend.scripts.bootstrap_public_skills`（M11 Task 16）。

把旧文件系统里的 public/custom 技能灌进 Git 目录：每个技能都走**和普通用户完全一样**的
生命周期（create_draft → put_file → save → submit → approve），所以状态机、审核记录、
公共 release 与 outbox 事件都齐全，不会出现只有 Git 没有 catalog 的半个技能。

安全底线：
- 只做**文件字节读取**。源目录里的脚本永远不执行：没有 shell、没有 import、没有 hook。
- 只接受相对路径；符号链接直接拒绝（和 Git 导入路径同一套校验）。
- 没有 `--confirm` 拒绝运行：这是往公共分支写东西的运维动作，不接受"手滑"。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"


@dataclass(frozen=True, slots=True)
class SourceSkill:
    """磁盘上的一个技能目录：名字来自目录名，内容只有文件字节。"""

    name: str
    directory: Path
    files: tuple[tuple[str, bytes, str], ...]


def _description_from_skill_md(content: bytes, fallback: str) -> str:
    """从 frontmatter 里抄一行 description；不做 YAML 解析，也绝不执行任何东西。"""

    text = content.decode("utf-8", "replace")
    if not text.startswith("---"):
        return fallback
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.lower().startswith("description:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            return value or fallback
    return fallback


def discover(source: Path) -> list[SourceSkill]:
    """扫描 public/ 与 custom/（没有这两个目录就扫描直接子目录）。"""

    roots: list[Path] = []
    for child in ("public", "custom"):
        candidate = source / child
        if candidate.is_dir() and not candidate.is_symlink():
            roots.append(candidate)
    if not roots:
        roots = [source]

    skills: list[SourceSkill] = []
    for root in roots:
        for entry in sorted(root.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not (entry / SKILL_FILE_NAME).is_file():
                continue
            skills.append(
                SourceSkill(
                    name=entry.name,
                    directory=entry,
                    files=tuple(_read_skill_files(entry)),
                )
            )
    return skills


def _read_skill_files(directory: Path) -> list[tuple[str, bytes, str]]:
    """只读字节。相对路径必须留在技能目录内，符号链接一律拒绝。"""

    files: list[tuple[str, bytes, str]] = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        base = Path(current)
        dirnames[:] = sorted(
            name for name in dirnames if not (base / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = base / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative = PurePosixPath(path.relative_to(directory).as_posix())
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"refusing non-relative skill path: {relative}")
            executable = bool(path.stat().st_mode & 0o111)
            files.append(
                (
                    relative.as_posix(),
                    path.read_bytes(),
                    "executable" if executable else "text",
                )
            )
    return files


async def bootstrap(
    *,
    store: object,
    author: object,
    review: object,
    source: Path,
    admin_user_id: str,
    apply: bool = True,
) -> int:
    """每个技能一个完整生命周期；返回成功创建并批准的技能数。"""

    created = 0
    for skill in discover(source):
        skill_md = next(
            (content for path, content, _mode in skill.files if path == SKILL_FILE_NAME),
            b"",
        )
        description = _description_from_skill_md(skill_md, skill.name)
        if not apply:
            print(f"[dry-run] would bootstrap {skill.name} ({len(skill.files)} file(s))")
            created += 1
            continue
        record = await author.create_draft(
            owner_user_id=admin_user_id,
            name=skill.name,
            description=description,
        )
        for path, content, mode in skill.files:
            await author.put_file(
                owner_user_id=admin_user_id,
                skill_id=record.id,
                path=path,
                content=content,
                mode=mode,
            )
        await author.save(owner_user_id=admin_user_id, skill_id=record.id)
        submission = await author.submit(
            owner_user_id=admin_user_id, skill_id=record.id
        )
        release = await review.approve(
            admin_user_id=admin_user_id, submission_id=submission.id
        )
        created += 1
        print(
            f"bootstrapped {skill.name}: skill_id={record.id} "
            f"release_id={release.id} ({len(skill.files)} file(s))"
        )
    return created


async def run(*, source: Path, admin_user_id: str, apply: bool) -> int:
    from agentkit.config import get_app_config
    from agentkit.config.runtime_paths import resolve_path
    from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
    from agentkit.skills.author import SkillAuthorService
    from agentkit.skills.content import SkillContentStore
    from agentkit.skills.git import SkillGitRepository
    from agentkit.skills.review import SkillReviewService
    from agentkit.skills.store.sql import SqlSkillStore

    config = get_app_config()
    await init_engine_from_config(config.database)
    try:
        skills = config.skills
        # 和网关/沙箱同一套解析：相对路径相对**项目根**，不是当前工作目录。
        # 否则在别的目录跑一次 bootstrap 就会多出一个裸镜像和提交缓存，网关再
        # 自己克隆一份，磁盘状态和文档说的不是一回事。
        cache_dir = resolve_path(skills.git.cache_dir)
        git = SkillGitRepository(skills.git, cache_dir)
        await git.ensure_ready()
        content = SkillContentStore(cache_dir, git)
        store = SqlSkillStore(get_session_factory())
        author = SkillAuthorService(store, git, content, skills.creation)
        review = SkillReviewService(store, git, content, skills.review)
        count = await bootstrap(
            store=store,
            author=author,
            review=review,
            source=source,
            admin_user_id=admin_user_id,
            apply=apply,
        )
        print(f"{count} skill(s) bootstrapped by {admin_user_id}")
        return 0
    finally:
        await close_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap public/custom skills into the Git catalog (M11 Task 16)."
    )
    parser.add_argument("--source", required=True, help="directory holding public/ and custom/")
    parser.add_argument("--admin-user-id", required=True, help="admin that owns and approves")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required: this publishes every discovered skill to the public branch",
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be published")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"source directory does not exist: {source}", flush=True)
        return 2
    # --dry-run 只是列出将要发布的技能，不写任何东西；--confirm 只有真正落库落库
    # 才需要。否则"先跑 --dry-run 看看"这条建议根本执行不了。
    if not args.confirm and not args.dry_run:
        print(
            "refusing to run without --confirm: bootstrapping publishes every discovered "
            "skill to the public branch. Re-run with --dry-run to list them, then with "
            "--confirm to publish.",
            flush=True,
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(
        run(
            source=source,
            admin_user_id=args.admin_user_id,
            apply=not args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
