"""技能包的路径校验、稳定哈希与初始 SKILL.md 渲染。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

import yaml

from agentkit.skills.domain import (
    InvalidSkillPackage,
    PackageManifest,
    StagedFile,
)
from agentkit.skills.parser import SkillParseError, parse_skill_text
from agentkit.skills.types import SKILL_FILE_NAME

_NUL = b"\x00"


def validate_skill_path(path: str) -> str:
    """校验并返回一个规范、相对的 POSIX 技能包路径。

    技能包路径来自用户输入，必须在写入 Git 或缓存目录之前拒绝所有可能逃逸
    技能目录的形式。合法路径按原样返回；调用方不应依赖隐式路径归一化。
    """

    if not isinstance(path, str) or not path:
        raise InvalidSkillPackage("skill path must be a non-empty string")
    if "\x00" in path:
        raise InvalidSkillPackage("skill path must not contain NUL")
    if "\\" in path:
        raise InvalidSkillPackage(f"skill path must use '/' separators: {path!r}")
    if path.startswith("/") or PurePosixPath(path).is_absolute():
        raise InvalidSkillPackage(f"skill path must be relative: {path!r}")

    windows_path = PureWindowsPath(path)
    if windows_path.is_absolute() or windows_path.drive:
        raise InvalidSkillPackage(f"skill path must be relative: {path!r}")

    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise InvalidSkillPackage(f"invalid skill path segment in {path!r}")
    return path


def build_package(
    files: Mapping[str, StagedFile],
    *,
    max_files: int,
    max_file_bytes: int,
    max_package_bytes: int,
) -> PackageManifest:
    """校验文件集合，计算规范哈希，并解析根目录的 SKILL.md。

    哈希输入严格为按 POSIX 相对路径排序后的
    path + NUL + mode + NUL + content + NUL。映射的迭代顺序、文件系统枚举
    顺序和平台换行都不参与计算。
    """

    entries = list(files.items())
    if len(entries) > max_files:
        raise InvalidSkillPackage(f"too many files: {len(entries)} > {max_files}")

    seen: set[str] = set()
    normalized: list[StagedFile] = []
    total_bytes = 0
    for key, staged in entries:
        if not isinstance(staged, StagedFile):
            raise InvalidSkillPackage("package entries must be StagedFile values")
        if not isinstance(staged.content, bytes):
            raise InvalidSkillPackage(f"file content must be bytes: {key!r}")

        path = validate_skill_path(key)
        staged_path = validate_skill_path(staged.path)
        if path != staged_path:
            raise InvalidSkillPackage(
                f"mapping key and staged path disagree: {path!r} != {staged_path!r}"
            )
        if path in seen:
            raise InvalidSkillPackage(f"duplicate skill path: {path!r}")

        size = len(staged.content)
        if size > max_file_bytes:
            raise InvalidSkillPackage(
                f"file too large: {path!r} is {size} bytes > {max_file_bytes}"
            )
        total_bytes += size
        if total_bytes > max_package_bytes:
            raise InvalidSkillPackage(
                f"package too large: {total_bytes} bytes > {max_package_bytes}"
            )

        seen.add(path)
        normalized.append(StagedFile(path=path, content=staged.content, mode=staged.mode))

    if SKILL_FILE_NAME not in seen:
        raise InvalidSkillPackage(f"package is missing {SKILL_FILE_NAME}")

    normalized.sort(key=lambda item: item.path)
    skill_file = next(item for item in normalized if item.path == SKILL_FILE_NAME)
    try:
        text = skill_file.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSkillPackage(f"{SKILL_FILE_NAME} must be UTF-8") from exc
    try:
        parsed = parse_skill_text(text)
    except (SkillParseError, yaml.YAMLError) as exc:
        raise InvalidSkillPackage(f"invalid {SKILL_FILE_NAME}: {exc}") from exc

    hasher = hashlib.sha256()
    for item in normalized:
        hasher.update(item.path.encode("utf-8"))
        hasher.update(_NUL)
        hasher.update(item.mode.encode("utf-8"))
        hasher.update(_NUL)
        hasher.update(item.content)
        hasher.update(_NUL)

    return PackageManifest(
        files=tuple(normalized),
        package_sha256=hasher.hexdigest(),
        skill_md_sha256=hashlib.sha256(skill_file.content).hexdigest(),
        parsed=parsed,
    )


def render_skill_md(name: str, description: str) -> str:
    """生成只含 name/description 的初始 SKILL.md frontmatter。"""

    frontmatter = yaml.safe_dump(
        {"name": name, "description": description},
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return f"---\n{frontmatter}\n---\n"


__all__ = [
    "InvalidSkillPackage",
    "PackageManifest",
    "StagedFile",
    "build_package",
    "render_skill_md",
    "validate_skill_path",
]
